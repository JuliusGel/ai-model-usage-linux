"""Fixture-based tests for the Claude usage parser."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_usage_indicator.telemetry import (
    Confidence,
    Source,
    TelemetryValidationError,
)
from ai_usage_indicator.telemetry_parsers import snapshot_from_claude_usage

OBSERVED = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def parse(fixture, name):
    return snapshot_from_claude_usage(fixture("claude", name), observed_at=OBSERVED)


def test_five_hour_and_seven_day(fixture):
    tel = parse(fixture, "five_hour_seven_day.json")
    assert tel.provider == "claude"
    assert tel.source is Source.OAUTH_API
    assert tel.confidence is Confidence.AUTHORITATIVE
    assert tel.observed_at == OBSERVED

    by_id = {w.id: w for w in tel.windows}
    assert set(by_id) == {"five_hour", "seven_day"}

    five = by_id["five_hour"]
    assert five.name == "5-hour"
    assert five.used_fraction == 0.45
    assert five.remaining_fraction == 0.55
    assert five.duration_seconds == 5 * 3600
    assert five.resets_at == datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)

    seven = by_id["seven_day"]
    assert seven.used_fraction == 0.30
    assert seven.duration_seconds == 7 * 86400


def test_model_specific_weekly_window(fixture):
    tel = parse(fixture, "with_opus_weekly.json")
    by_id = {w.id: w for w in tel.windows}
    assert set(by_id) == {"five_hour", "seven_day", "seven_day_opus"}

    opus = by_id["seven_day_opus"]
    assert opus.name == "7-day (opus)"
    assert opus.used_fraction == 0.88
    assert opus.duration_seconds == 7 * 86400
    # The model-specific weekly limit is the most-constrained -> the natural headline.
    assert tel.most_constrained().id == "seven_day_opus"


def test_round_trips_through_envelope(fixture):
    from ai_usage_indicator.telemetry import Telemetry

    tel = parse(fixture, "five_hour_seven_day.json")
    assert Telemetry.from_envelope(tel.to_envelope()) == tel


def test_invalid_utilization_over_100_raises(fixture):
    with pytest.raises(TelemetryValidationError):
        parse(fixture, "invalid_utilization_over_100.json")


def test_invalid_naive_reset_raises(fixture):
    with pytest.raises(TelemetryValidationError):
        parse(fixture, "invalid_reset_naive.json")


def test_empty_payload_raises():
    with pytest.raises(TelemetryValidationError):
        snapshot_from_claude_usage({}, observed_at=OBSERVED)


def test_non_dict_payload_raises():
    with pytest.raises(TelemetryValidationError):
        snapshot_from_claude_usage([], observed_at=OBSERVED)
