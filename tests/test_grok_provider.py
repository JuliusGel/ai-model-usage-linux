"""Grok adapter: CLI-start and OIDC access-token refresh against auth.json."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ai_usage_indicator.net import HttpError
from ai_usage_indicator.providers.grok import GrokProvider, LocalSpend
from ai_usage_indicator.telemetry import Source


AUTH_KEY = "https://auth.x.ai::client-1"


def _write_auth(path, **overrides) -> None:
    entry = {
        "key": "old-token",
        "user_id": "user-1",
        "principal_type": "User",
        "expires_at": "2099-01-01T00:00:00Z",
        "refresh_token": "refresh-me",
        "oidc_issuer": "https://auth.x.ai",
        "oidc_client_id": "client-1",
        "email": "dev@example.com",
    }
    entry.update(overrides)
    path.write_text(json.dumps({AUTH_KEY: entry}, indent=2, ensure_ascii=False) + "\n")


def _auth_entry(path) -> dict:
    return json.loads(path.read_text())[AUTH_KEY]


@pytest.fixture(autouse=True)
def no_cli_refresh(monkeypatch):
    """OIDC tests must not spawn a real ``grok`` from PATH."""
    monkeypatch.setattr(
        "ai_usage_indicator.providers.grok._invoke_grok_refresh",
        lambda *_args, **_kwargs: None,
    )


def test_unexpired_token_does_not_refresh_or_rewrite_auth(tmp_path, monkeypatch, fixture):
    auth = tmp_path / "auth.json"
    _write_auth(auth)
    before = auth.read_bytes()
    posts: list[tuple] = []

    def fake_post(url, headers, fields, timeout=20.0):
        posts.append((url, fields))
        raise AssertionError("refresh must not run for a still-valid token")

    monkeypatch.setattr(
        "ai_usage_indicator.providers.grok.get_json",
        lambda _url, _headers: fixture("grok", "weekly_credits.json"),
    )
    monkeypatch.setattr("ai_usage_indicator.providers.grok.post_form", fake_post)

    telemetry = GrokProvider(config={"auth_path": str(auth)}).fetch_telemetry()
    assert telemetry.source is Source.OAUTH_API
    assert posts == []
    assert auth.read_bytes() == before


def test_expired_token_refreshes_via_oidc_and_persists(tmp_path, monkeypatch, fixture):
    auth = tmp_path / "auth.json"
    _write_auth(auth, expires_at="2020-01-01T00:00:00Z")
    captured = {}

    def fake_post(url, headers, fields, timeout=20.0):
        captured["url"] = url
        captured["fields"] = fields
        return {
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }

    def fake_get(url, headers):
        captured["billing_auth"] = headers.get("Authorization")
        return fixture("grok", "weekly_credits.json")

    monkeypatch.setattr("ai_usage_indicator.providers.grok.get_json", fake_get)
    monkeypatch.setattr("ai_usage_indicator.providers.grok.post_form", fake_post)

    telemetry = GrokProvider(config={"auth_path": str(auth)}).fetch_telemetry()
    assert telemetry.source is Source.OAUTH_API
    assert captured["url"] == "https://auth.x.ai/oauth2/token"
    assert captured["fields"] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-me",
        "client_id": "client-1",
    }
    assert captured["billing_auth"] == "Bearer new-token"

    saved = _auth_entry(auth)
    assert saved["key"] == "new-token"
    assert saved["refresh_token"] == "new-refresh"
    assert saved["email"] == "dev@example.com"
    assert datetime.fromisoformat(saved["expires_at"].replace("Z", "+00:00")) > datetime.now(
        timezone.utc
    )
    assert auth.stat().st_mode & 0o777 == 0o600


def test_expired_token_is_refreshed_by_starting_grok(tmp_path, monkeypatch, fixture):
    auth = tmp_path / "auth.json"
    _write_auth(auth, expires_at="2020-01-01T00:00:00Z")
    invoked = {}

    def fake_cli(command, auth_path, timeout):
        invoked["command"] = command
        invoked["home"] = str(auth_path.parent)
        _write_auth(auth, key="cli-token", expires_at="2099-01-01T00:00:00Z")

    monkeypatch.setattr("ai_usage_indicator.providers.grok._invoke_grok_refresh", fake_cli)
    monkeypatch.setattr(
        "ai_usage_indicator.providers.grok.post_form",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OIDC must not run when starting grok refreshed the token")
        ),
    )

    def fake_get(url, headers):
        assert headers["Authorization"] == "Bearer cli-token"
        return fixture("grok", "weekly_credits.json")

    monkeypatch.setattr("ai_usage_indicator.providers.grok.get_json", fake_get)
    grok_bin = tmp_path / "bin" / "grok"
    grok_bin.parent.mkdir()
    grok_bin.write_text("#!/bin/sh\n")
    grok_bin.chmod(0o755)

    telemetry = GrokProvider(
        config={"auth_path": str(auth), "command": str(grok_bin)}
    ).fetch_telemetry()
    assert telemetry.source is Source.OAUTH_API
    assert invoked["command"] == str(grok_bin)
    assert invoked["home"] == str(tmp_path)


def test_expired_token_without_refresh_token_asks_to_start_grok(
    tmp_path, monkeypatch, fixture
):
    auth = tmp_path / "auth.json"
    _write_auth(auth, expires_at="2020-01-01T00:00:00Z", refresh_token="")
    monkeypatch.setattr(
        "ai_usage_indicator.providers.grok.get_json",
        lambda *_args, **_kwargs: fixture("grok", "weekly_credits.json"),
    )
    monkeypatch.setattr(
        "ai_usage_indicator.providers.grok.post_form",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not refresh")),
    )
    record = GrokProvider(config={"auth_path": str(auth)}).safe_fetch()
    assert record.error is not None
    assert "run `grok`" in record.error
    assert "login" not in record.error


def test_billing_401_refreshes_once_and_retries(tmp_path, monkeypatch, fixture):
    auth = tmp_path / "auth.json"
    _write_auth(auth)
    calls = {"billing": 0}

    def fake_get(url, headers):
        calls["billing"] += 1
        if calls["billing"] == 1:
            assert headers["Authorization"] == "Bearer old-token"
            raise HttpError(401, "expired")
        assert headers["Authorization"] == "Bearer new-token"
        return fixture("grok", "weekly_credits.json")

    def fake_post(url, headers, fields, timeout=20.0):
        return {"access_token": "new-token", "expires_in": 21600}

    monkeypatch.setattr("ai_usage_indicator.providers.grok.get_json", fake_get)
    monkeypatch.setattr("ai_usage_indicator.providers.grok.post_form", fake_post)

    telemetry = GrokProvider(config={"auth_path": str(auth)}).fetch_telemetry()
    assert telemetry.source is Source.OAUTH_API
    assert calls["billing"] == 2
    assert _auth_entry(auth)["key"] == "new-token"
    assert _auth_entry(auth)["refresh_token"] == "refresh-me"


def test_failed_refresh_adopts_sibling_token_from_disk(tmp_path, monkeypatch, fixture):
    auth = tmp_path / "auth.json"
    _write_auth(auth, expires_at="2020-01-01T00:00:00Z")

    def fake_post(url, headers, fields, timeout=20.0):
        _write_auth(
            auth,
            key="sibling-token",
            refresh_token="sibling-refresh",
            expires_at="2099-01-01T00:00:00Z",
        )
        raise HttpError(400, "invalid_grant")

    def fake_get(url, headers):
        assert headers["Authorization"] == "Bearer sibling-token"
        return fixture("grok", "weekly_credits.json")

    monkeypatch.setattr("ai_usage_indicator.providers.grok.get_json", fake_get)
    monkeypatch.setattr("ai_usage_indicator.providers.grok.post_form", fake_post)

    telemetry = GrokProvider(config={"auth_path": str(auth)}).fetch_telemetry()
    assert telemetry.source is Source.OAUTH_API


def test_refresh_http_failure_without_sibling_asks_to_start_grok(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    _write_auth(auth, expires_at="2020-01-01T00:00:00Z")
    monkeypatch.setattr(
        "ai_usage_indicator.providers.grok.post_form",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HttpError(400, "invalid_grant")),
    )
    record = GrokProvider(config={"auth_path": str(auth)}).safe_fetch()
    assert record.error is not None
    assert "run `grok`" in record.error
    assert "login" not in record.error


# --------------------------------------------------------- xAI console (Management API)


def _console_transport(monkeypatch, fixture, *, billing="team_no_percent.json"):
    """Route the CLI billing GET, the console invoice GET, and the console usage POST."""
    calls: dict[str, object] = {}

    def fake_get(url, headers, timeout=20.0):
        if url.startswith("https://management-api.x.ai"):
            calls["invoice_url"] = url
            calls["invoice_auth"] = headers.get("Authorization")
            return fixture("grok", "console_invoice_preview.json")
        return fixture("grok", billing)

    def fake_post(url, headers, body, timeout=20.0):
        calls["usage_url"] = url
        calls["usage_body"] = body
        calls["usage_auth"] = headers.get("Authorization")
        return fixture("grok", "console_usage.json")

    monkeypatch.setattr("ai_usage_indicator.providers.grok.get_json", fake_get)
    monkeypatch.setattr("ai_usage_indicator.providers.grok.post_json", fake_post)
    return calls


def test_team_uses_console_spend_and_credits(tmp_path, monkeypatch, fixture):
    auth = tmp_path / "auth.json"
    _write_auth(auth, principal_type="Team", team_id="team-1")
    calls = _console_transport(monkeypatch, fixture)

    provider = GrokProvider(
        "xai", {"auth_path": str(auth), "management_key": "xai-mgmt-key"}
    )
    record = provider.safe_fetch()

    assert record.error is None
    assert record.used == pytest.approx(8.1138438612)
    assert record.limit == pytest.approx(253.51)
    assert record.unit == "USD"
    assert record.label == "$8.11 / $253.51"
    assert record.percent == 3
    assert record.reset_at == datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert calls["usage_url"].endswith("/v1/billing/teams/team-1/usage")
    assert calls["invoice_url"].endswith(
        "/v1/billing/teams/team-1/postpaid/invoice/preview"
    )
    assert calls["usage_auth"] == "Bearer xai-mgmt-key"
    assert calls["invoice_auth"] == "Bearer xai-mgmt-key"


def test_console_request_covers_the_billing_period(tmp_path, monkeypatch, fixture):
    auth = tmp_path / "auth.json"
    _write_auth(auth, principal_type="Team", team_id="team-1")
    calls = _console_transport(monkeypatch, fixture)

    GrokProvider("xai", {"auth_path": str(auth), "management_key": "k"}).safe_fetch()

    time_range = calls["usage_body"]["analyticsRequest"]["timeRange"]
    assert time_range["startTime"] == "2026-09-08 00:00:00"
    assert time_range["endTime"] == "2026-09-15 00:00:00"
    assert time_range["timezone"] == "Etc/GMT"


def test_console_telemetry_is_authoritative(tmp_path, monkeypatch, fixture):
    auth = tmp_path / "auth.json"
    _write_auth(auth, principal_type="Team", team_id="team-1")
    _console_transport(monkeypatch, fixture)

    telemetry = GrokProvider(
        "xai", {"auth_path": str(auth), "management_key": "k"}
    ).fetch_telemetry()

    assert telemetry.source is Source.MANAGEMENT_API
    assert telemetry.windows[0].used_fraction == pytest.approx(0.032006, abs=1e-6)


def test_management_key_read_from_environment(tmp_path, monkeypatch, fixture):
    auth = tmp_path / "auth.json"
    _write_auth(auth, principal_type="Team", team_id="team-1")
    calls = _console_transport(monkeypatch, fixture)
    monkeypatch.setenv("XAI_MANAGEMENT_KEY", "from-env")

    GrokProvider("xai", {"auth_path": str(auth)}).safe_fetch()

    assert calls["usage_auth"] == "Bearer from-env"


def test_configured_team_id_overrides_auth_json(tmp_path, monkeypatch, fixture):
    auth = tmp_path / "auth.json"
    _write_auth(auth, principal_type="Team", team_id="team-1")
    calls = _console_transport(monkeypatch, fixture)

    GrokProvider(
        "xai",
        {"auth_path": str(auth), "management_key": "k", "team_id": "team-override"},
    ).safe_fetch()

    assert calls["usage_url"].endswith("/v1/billing/teams/team-override/usage")


def test_plan_percent_wins_over_console(tmp_path, monkeypatch, fixture):
    """A SuperGrok account reports its own percent; don't spend calls on the console."""
    auth = tmp_path / "auth.json"
    _write_auth(auth)
    calls = _console_transport(monkeypatch, fixture, billing="weekly_credits.json")

    record = GrokProvider(
        "xai", {"auth_path": str(auth), "management_key": "k"}
    ).safe_fetch()

    assert record.label == "wk 42% · build 61% · on-demand 6%"
    assert "usage_url" not in calls


def test_without_management_key_team_falls_back_to_local_spend(
    tmp_path, monkeypatch, fixture
):
    auth = tmp_path / "auth.json"
    _write_auth(auth, principal_type="Team", team_id="team-1")
    monkeypatch.setattr(
        "ai_usage_indicator.providers.grok.get_json",
        lambda _url, _headers: fixture("grok", "team_no_percent.json"),
    )
    monkeypatch.setattr(
        "ai_usage_indicator.providers.grok.scan_local_usage",
        lambda _root, _start, _end: LocalSpend(6.0378, 4_728_495, 1),
    )

    record = GrokProvider(
        "xai", {"auth_path": str(auth), "allowance_usd": 150}
    ).safe_fetch()

    assert record.used == pytest.approx(6.0378)
    assert record.limit == 150


def test_console_failure_is_reported_not_masked_by_local_spend(
    tmp_path, monkeypatch, fixture
):
    """A stale local ledger must never stand in for a broken console call."""
    auth = tmp_path / "auth.json"
    _write_auth(auth, principal_type="Team", team_id="team-1")
    monkeypatch.setattr(
        "ai_usage_indicator.providers.grok.get_json",
        lambda _url, _headers: fixture("grok", "team_no_percent.json"),
    )

    def fake_post(_url, _headers, _body, timeout=20.0):
        raise HttpError(401, "invalid key")

    monkeypatch.setattr("ai_usage_indicator.providers.grok.post_json", fake_post)

    record = GrokProvider(
        "xai",
        {"auth_path": str(auth), "management_key": "bad", "allowance_usd": 150},
    ).safe_fetch()

    assert record.error == "management key rejected — recreate it at console.x.ai"


def test_allowance_backs_a_console_without_credits(tmp_path, monkeypatch, fixture):
    auth = tmp_path / "auth.json"
    _write_auth(auth, principal_type="Team", team_id="team-1")

    def fake_get(url, headers, timeout=20.0):
        if url.startswith("https://management-api.x.ai"):
            return {"billingCycle": {"year": 2026, "month": 9}}
        return fixture("grok", "team_no_percent.json")

    monkeypatch.setattr("ai_usage_indicator.providers.grok.get_json", fake_get)
    monkeypatch.setattr(
        "ai_usage_indicator.providers.grok.post_json",
        lambda _url, _headers, _body, timeout=20.0: fixture("grok", "console_usage.json"),
    )

    record = GrokProvider(
        "xai",
        {"auth_path": str(auth), "management_key": "k", "allowance_usd": 150},
    ).safe_fetch()

    assert record.limit == 150
    assert record.label == "$8.11 / $150.00"
