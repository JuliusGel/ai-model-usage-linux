"""Tests for the shared API, provider wiring, and GNOME compatibility adapter."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import ai_model_usage
from ai_model_usage.__main__ import main as telemetry_main
from ai_usage_indicator import config as config_module
from ai_usage_indicator.config import Config
from ai_usage_indicator.core import TelemetryCollection, collect_telemetry
from ai_usage_indicator.providers.base import Provider
from ai_usage_indicator.providers.claude import ClaudeProvider
from ai_usage_indicator.providers.codex import CodexProvider
from ai_usage_indicator.providers.grok import GrokProvider
from ai_usage_indicator.state import build_state
from ai_usage_indicator.telemetry import (
    Confidence,
    QuotaWindow,
    Source,
    Telemetry,
    TelemetryValidationError,
)
from ai_usage_indicator.usage import usage_from_telemetry

OBSERVED = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def snapshot(provider: str = "test") -> Telemetry:
    return Telemetry(
        provider=provider,
        observed_at=OBSERVED,
        source=Source.FIXTURE,
        confidence=Confidence.AUTHORITATIVE,
        windows=[
            QuotaWindow.from_used_fraction(
                id="short",
                name="5-hour",
                used_fraction=0.30,
                duration_seconds=5 * 3600,
                resets_at=OBSERVED + timedelta(hours=2),
            ),
            QuotaWindow.from_used_fraction(
                id="long",
                name="Weekly",
                used_fraction=0.80,
                duration_seconds=7 * 86400,
                resets_at=OBSERVED + timedelta(days=4),
            ),
        ],
    )


class StaticProvider(Provider):
    def __init__(self, provider_id: str = "test", *, error: Exception | None = None):
        super().__init__()
        self.id = provider_id
        self.display_name = provider_id.title()
        self.error = error

    def authenticate(self) -> None:
        return None

    def fetch_telemetry(self) -> Telemetry:
        if self.error is not None:
            raise self.error
        return snapshot(self.id)


def test_public_package_exports_typed_python_api():
    collection = ai_model_usage.collect_telemetry([StaticProvider()])
    assert isinstance(collection, ai_model_usage.TelemetryCollection)
    assert isinstance(collection.telemetry[0], ai_model_usage.Telemetry)


def test_collection_uses_versioned_t2_json_shape():
    envelope = collect_telemetry([StaticProvider()]).to_envelope()
    assert envelope["schema_version"] == ai_model_usage.SCHEMA_VERSION
    assert envelope["telemetry"] == [snapshot().to_dict()]
    assert envelope["errors"] == []


def test_collection_isolates_provider_errors():
    collection = collect_telemetry(
        [
            StaticProvider("good"),
            StaticProvider("bad", error=TelemetryValidationError("bad payload")),
        ]
    )
    assert [item.provider for item in collection.telemetry] == ["good"]
    assert collection.errors[0].provider == "bad"
    assert collection.errors[0].error == "bad payload"


def test_safe_fetch_wraps_parser_errors_for_gnome():
    record = StaticProvider(
        error=TelemetryValidationError("fraction out of range")
    ).safe_fetch()
    assert record.error == "fraction out of range"
    assert record.percent is None


def test_usage_projection_uses_most_constrained_window():
    telemetry = snapshot("claude")
    record = usage_from_telemetry(telemetry, display_name="Claude")
    assert telemetry.most_constrained().id == "long"
    assert record.percent == 80
    assert record.reset_at == telemetry.windows[1].resets_at
    assert record.label == "5h 30% · wk 80%"


def test_legacy_state_json_keys_are_preserved():
    state = build_state([usage_from_telemetry(snapshot("codex"), display_name="Codex")])
    assert set(state) == {"updated_at", "providers"}
    assert set(state["providers"][0]) == {
        "id",
        "display_name",
        "percent",
        "pressure",
        "label",
        "detail",
        "reset_text",
        "error",
    }


def test_read_only_config_load_does_not_create_file(tmp_path, monkeypatch):
    path = tmp_path / "missing" / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    config = config_module.load_config(create=False)
    assert [entry["type"] for entry in config.providers] == ["claude", "codex", "grok"]
    assert not path.exists()


def test_collect_accepts_explicit_config_without_json_round_trip():
    collection = collect_telemetry(
        config=Config(providers=[{"id": "demo", "type": "mock", "used": 25, "limit": 100}])
    )
    assert collection.telemetry[0].provider == "demo"
    assert collection.telemetry[0].windows[0].used_fraction == 0.25


def test_claude_provider_wires_raw_payload_to_canonical_parser(
    tmp_path, monkeypatch, fixture
):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "not-a-real-token",
                    "expiresAt": 4102444800000,
                }
            }
        )
    )
    before = credentials.read_bytes()
    monkeypatch.setattr(
        "ai_usage_indicator.providers.claude.get_json",
        lambda _url, _headers: fixture("claude", "with_opus_weekly.json"),
    )
    provider = ClaudeProvider(config={"credentials_path": str(credentials)})
    telemetry = provider.fetch_telemetry()
    assert [window.id for window in telemetry.windows] == [
        "five_hour",
        "seven_day",
        "seven_day_opus",
    ]
    assert telemetry.most_constrained().id == "seven_day_opus"
    assert credentials.read_bytes() == before


def test_claude_safe_fetch_contains_live_parser_failure(tmp_path, monkeypatch, fixture):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "not-a-real-token",
                    "expiresAt": 4102444800000,
                }
            }
        )
    )
    monkeypatch.setattr(
        "ai_usage_indicator.providers.claude.get_json",
        lambda _url, _headers: fixture(
            "claude", "invalid_utilization_over_100.json"
        ),
    )
    record = ClaudeProvider(config={"credentials_path": str(credentials)}).safe_fetch()
    assert record.error is not None
    assert "out of range" in record.error


def test_codex_provider_wires_app_server_payload_to_canonical_parser(
    monkeypatch, fixture
):
    monkeypatch.setattr(
        "ai_usage_indicator.providers.codex._read_app_server_rate_limits",
        lambda **_kwargs: fixture("codex", "primary_and_secondary.json"),
    )
    telemetry = CodexProvider().fetch_telemetry()
    assert telemetry.source is Source.APP_SERVER
    assert telemetry.plan == "pro"
    assert telemetry.most_constrained().id == "secondary"


def test_grok_provider_wires_raw_payload_to_canonical_parser(
    tmp_path, monkeypatch, fixture
):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "https://auth.x.ai::test": {
                    "key": "not-a-real-token",
                    "user_id": "user-1",
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            }
        )
    )
    before = auth.read_bytes()
    monkeypatch.setattr(
        "ai_usage_indicator.providers.grok.get_json",
        lambda _url, _headers: fixture("grok", "weekly_credits.json"),
    )
    provider = GrokProvider(config={"auth_path": str(auth)})
    telemetry = provider.fetch_telemetry()
    assert telemetry.source is Source.OAUTH_API
    assert telemetry.plan == "SuperGrok"
    assert telemetry.most_constrained().id == "product_grok_build"
    assert auth.read_bytes() == before


def test_json_cli_serializes_same_collection(monkeypatch, capsys):
    expected = TelemetryCollection(telemetry=[snapshot()])
    monkeypatch.setattr("ai_model_usage.__main__.collect_telemetry", lambda: expected)
    assert telemetry_main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == expected.to_envelope()


def test_json_cli_returns_nonzero_but_keeps_machine_readable_errors(
    monkeypatch, capsys
):
    expected = collect_telemetry(
        [StaticProvider(error=TelemetryValidationError("invalid provider data"))]
    )
    monkeypatch.setattr("ai_model_usage.__main__.collect_telemetry", lambda: expected)
    assert telemetry_main(["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["errors"][0]["error"] == (
        "invalid provider data"
    )
