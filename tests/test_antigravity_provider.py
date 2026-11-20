"""Tests for AntigravityProvider keyring auth, CLI refresh, and PATH-free lookup."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from ai_usage_indicator.net import HttpError
from ai_usage_indicator.providers import antigravity as agy_mod
from ai_usage_indicator.providers.antigravity import (
    AntigravityProvider,
    _extract_token,
    _parse_expiry,
    resolve_agy_command,
)
from ai_usage_indicator.providers.base import ProviderError


def _keyring_blob(access_token: str = "tok-1", expiry: str = "2099-01-01T00:00:00.123456789+02:00") -> str:
    return json.dumps(
        {
            "auth_method": "consumer",
            "id_token_email": "user@example.com",
            "token": {
                "access_token": access_token,
                "token_type": "Bearer",
                "expiry": expiry,
            },
        }
    )


def test_parse_expiry_handles_go_rfc3339_nanoseconds():
    dt = _parse_expiry("2026-11-20T13:05:38.739767591+02:00")
    assert dt == datetime(2026, 11, 20, 11, 5, 38, 739767, tzinfo=timezone.utc)


def test_extract_token_supports_nested_and_flat_blobs():
    tok, exp = _extract_token(_keyring_blob("abc"))
    assert tok == "abc"
    assert exp is not None

    tok2, exp2 = _extract_token(json.dumps({"access_token": "flat-tok"}))
    assert tok2 == "flat-tok"
    assert exp2 is None


def test_fetch_telemetry_reads_keyring_and_parses_models(monkeypatch, fixture):
    monkeypatch.setattr(agy_mod, "_read_keyring_secret", lambda **_kw: _keyring_blob("valid-tok"))
    seen_headers: dict[str, str] = {}

    def fake_post(url, headers, body):
        seen_headers.update(headers)
        assert body == {}
        assert "fetchAvailableModels" in url
        return fixture("antigravity", "available_models.json")

    def fake_get(url, headers):
        assert headers["Authorization"] == "Bearer valid-tok"
        assert "fetchQuotaStatus" in url
        return fixture("antigravity", "fetch_quota_status.json")

    monkeypatch.setattr(agy_mod, "post_json", fake_post)
    monkeypatch.setattr(agy_mod, "get_json", fake_get)
    tel = AntigravityProvider().fetch_telemetry()
    assert seen_headers["Authorization"] == "Bearer valid-tok"
    assert [w.id for w in tel.windows] == [
        "gemini",
        "gemini_weekly",
        "claude",
        "claude_weekly",
    ]


def test_fetch_quota_status_failure_falls_back_to_legacy_summary(monkeypatch, fixture):
    monkeypatch.setattr(agy_mod, "_read_keyring_secret", lambda **_kw: _keyring_blob("valid-tok"))

    def fake_post(url, _headers, _body):
        if "retrieveUserQuotaSummary" in url:
            return fixture("antigravity", "quota_summary.json")
        return fixture("antigravity", "available_models.json")

    def fake_get(_url, _headers):
        raise HttpError(404, "not found")

    monkeypatch.setattr(agy_mod, "post_json", fake_post)
    monkeypatch.setattr(agy_mod, "get_json", fake_get)
    tel = AntigravityProvider().fetch_telemetry()
    assert [w.id for w in tel.windows] == ["gemini", "claude", "claude_weekly"]


def test_expired_token_refreshes_via_agy_models(monkeypatch, fixture):
    state = {"raw": _keyring_blob("old-tok", expiry="2020-01-01T00:00:00Z"), "refreshes": 0}

    monkeypatch.setattr(agy_mod, "_read_keyring_secret", lambda **_kw: state["raw"])
    monkeypatch.setattr(agy_mod, "resolve_agy_command", lambda _cmd: "/fake/bin/agy")

    def fake_refresh(cmd, timeout=20.0):
        assert cmd == "/fake/bin/agy"
        state["refreshes"] += 1
        state["raw"] = _keyring_blob("fresh-tok", expiry="2099-01-01T00:00:00Z")

    monkeypatch.setattr(agy_mod, "_invoke_agy_refresh", fake_refresh)
    monkeypatch.setattr(
        agy_mod,
        "post_json",
        lambda _url, headers, _body: (
            fixture("antigravity", "available_models.json")
            if headers["Authorization"] == "Bearer fresh-tok"
            else (_ for _ in ()).throw(AssertionError("expected refreshed token"))
        ),
    )
    monkeypatch.setattr(
        agy_mod,
        "get_json",
        lambda _url, headers: (
            fixture("antigravity", "fetch_quota_status.json")
            if headers["Authorization"] == "Bearer fresh-tok"
            else (_ for _ in ()).throw(AssertionError("expected refreshed token"))
        ),
    )

    tel = AntigravityProvider().fetch_telemetry()
    assert state["refreshes"] == 1
    assert tel.most_constrained().id == "claude_weekly"


def test_401_triggers_single_refresh_and_retry(monkeypatch, fixture):
    state = {"raw": _keyring_blob("stale-tok"), "calls": 0}

    monkeypatch.setattr(agy_mod, "_read_keyring_secret", lambda **_kw: state["raw"])
    monkeypatch.setattr(agy_mod, "resolve_agy_command", lambda _cmd: "/fake/bin/agy")
    monkeypatch.setattr(
        agy_mod,
        "_invoke_agy_refresh",
        lambda _cmd, timeout=20.0: state.__setitem__("raw", _keyring_blob("new-tok")),
    )

    def fake_post(_url, headers, _body):
        state["calls"] += 1
        if headers["Authorization"] == "Bearer stale-tok":
            raise HttpError(401, "unauthorized")
        return fixture("antigravity", "available_models.json")

    def fake_get(_url, headers):
        state["calls"] += 1
        assert headers["Authorization"] == "Bearer new-tok"
        return fixture("antigravity", "fetch_quota_status.json")

    monkeypatch.setattr(agy_mod, "post_json", fake_post)
    monkeypatch.setattr(agy_mod, "get_json", fake_get)
    tel = AntigravityProvider().fetch_telemetry()
    assert state["calls"] == 3
    assert tel.provider == "antigravity"
    assert [w.id for w in tel.windows] == [
        "gemini",
        "gemini_weekly",
        "claude",
        "claude_weekly",
    ]


def test_expired_token_without_cli_raises(monkeypatch):
    monkeypatch.setattr(
        agy_mod, "_read_keyring_secret", lambda **_kw: _keyring_blob("old-tok", expiry="2020-01-01T00:00:00Z")
    )
    monkeypatch.setattr(agy_mod, "resolve_agy_command", lambda _cmd: None)
    with pytest.raises(ProviderError, match="token expired"):
        AntigravityProvider().authenticate()


def test_resolve_agy_command_finds_mise_fallback(tmp_path, monkeypatch):
    mise_dir = tmp_path / ".local" / "share" / "mise" / "installs" / "antigravity-cli" / "latest"
    mise_dir.mkdir(parents=True)
    agy_bin = mise_dir / "agy"
    agy_bin.write_text("#!/bin/sh\n")
    agy_bin.chmod(0o755)

    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(agy_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(agy_mod, "_SYSTEM_BIN_DIRS", ())
    assert resolve_agy_command("agy") == str(agy_bin)
