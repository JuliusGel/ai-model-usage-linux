"""Grok adapter: CLI-start and OIDC access-token refresh against auth.json."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ai_usage_indicator.net import HttpError
from ai_usage_indicator.providers.grok import GrokProvider
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
