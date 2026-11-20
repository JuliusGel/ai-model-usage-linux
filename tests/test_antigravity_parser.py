"""Fixture-based tests for the Antigravity fetchAvailableModels parser."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_usage_indicator.telemetry import (
    Confidence,
    Source,
    Telemetry,
    TelemetryValidationError,
)
from ai_usage_indicator.telemetry_parsers import snapshot_from_antigravity_models

OBSERVED = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def parse(fixture, name):
    return snapshot_from_antigravity_models(
        fixture("antigravity", name), observed_at=OBSERVED
    )


def test_collapses_shared_gemini_and_claude_gpt_pools(fixture):
    tel = parse(fixture, "available_models.json")
    assert tel.provider == "antigravity"
    assert tel.source is Source.OAUTH_API
    assert tel.confidence is Confidence.AUTHORITATIVE
    assert tel.observed_at == OBSERVED

    assert [w.id for w in tel.windows] == ["gemini", "claude"]

    gemini = tel.windows[0]
    assert gemini.name == "Gemini 5-hour"
    assert gemini.used_fraction == pytest.approx(1.0 - 0.9563093, abs=1e-6)
    assert gemini.remaining_fraction == pytest.approx(0.9563093, abs=1e-6)
    assert gemini.duration_seconds == 5 * 3600
    assert gemini.resets_at == datetime(2026, 11, 20, 15, 8, 5, tzinfo=timezone.utc)

    claude = tel.windows[1]
    assert claude.name == "Claude 5-hour"
    assert claude.used_fraction == 0.0
    assert claude.duration_seconds == 5 * 3600
    assert claude.resets_at == datetime(2026, 11, 20, 15, 11, 35, tzinfo=timezone.utc)

    assert tel.most_constrained().id == "gemini"


def test_includes_weekly_windows_from_quota_summary(fixture):
    tel = snapshot_from_antigravity_models(
        fixture("antigravity", "available_models.json"),
        summary=fixture("antigravity", "quota_summary.json"),
        observed_at=OBSERVED,
    )
    assert [w.id for w in tel.windows] == ["gemini", "claude", "claude_weekly"]
    weekly = tel.windows[2]
    assert weekly.name == "Claude wk"
    assert weekly.used_fraction == pytest.approx(0.35)
    assert weekly.duration_seconds == 7 * 86400
    assert weekly.resets_at == datetime(2026, 11, 27, 10, 19, 33, tzinfo=timezone.utc)
    assert tel.most_constrained().id == "claude_weekly"


def test_includes_gemini_and_claude_weekly_from_fetch_quota_status(fixture):
    tel = snapshot_from_antigravity_models(
        fixture("antigravity", "available_models.json"),
        summary=fixture("antigravity", "fetch_quota_status.json"),
        observed_at=OBSERVED,
    )
    assert [w.id for w in tel.windows] == [
        "gemini",
        "gemini_weekly",
        "claude",
        "claude_weekly",
    ]
    gemini_wk = tel.windows[1]
    assert gemini_wk.name == "Gemini wk"
    assert gemini_wk.used_fraction == pytest.approx(0.28)
    assert gemini_wk.duration_seconds == 7 * 86400
    assert gemini_wk.resets_at == datetime(2026, 11, 27, 10, 8, 5, tzinfo=timezone.utc)

    claude_wk = tel.windows[3]
    assert claude_wk.name == "Claude wk"
    assert claude_wk.used_fraction == pytest.approx(0.35)
    assert claude_wk.resets_at == datetime(2026, 11, 27, 15, 13, 24, tzinfo=timezone.utc)
    assert tel.most_constrained().id == "claude_weekly"


def test_preserves_distinct_pools_and_proto3_omitted_zero(fixture):
    tel = parse(fixture, "split_pools.json")
    by_id = {w.id: w for w in tel.windows}
    assert list(by_id) == ["gemini_pro", "gemini_flash", "claude", "gpt"]

    # Omitted remainingFraction alongside resetTime means exhausted (100% used).
    assert by_id["gemini_pro"].used_fraction == 1.0
    assert by_id["gemini_pro"].remaining_fraction == 0.0
    assert by_id["gemini_flash"].used_fraction == pytest.approx(0.2)
    assert by_id["claude"].used_fraction == pytest.approx(0.6)
    assert by_id["gpt"].used_fraction == pytest.approx(0.1)
    assert tel.most_constrained().id == "gemini_pro"


def test_round_trips_through_envelope(fixture):
    tel = parse(fixture, "available_models.json")
    assert Telemetry.from_envelope(tel.to_envelope()) == tel


def test_invalid_remaining_fraction_out_of_range_raises():
    payload = {
        "models": {
            "gemini-2.5-pro": {
                "displayName": "Gemini 2.5 Pro",
                "quotaInfo": {"remainingFraction": 1.5},
            }
        }
    }
    with pytest.raises(TelemetryValidationError):
        snapshot_from_antigravity_models(payload, observed_at=OBSERVED)


def test_empty_or_missing_models_raises():
    with pytest.raises(TelemetryValidationError):
        snapshot_from_antigravity_models({}, observed_at=OBSERVED)
    with pytest.raises(TelemetryValidationError):
        snapshot_from_antigravity_models({"models": {}}, observed_at=OBSERVED)
    with pytest.raises(TelemetryValidationError):
        snapshot_from_antigravity_models([], observed_at=OBSERVED)
