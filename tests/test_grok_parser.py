"""Fixture-based tests for the Grok billing parser."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_usage_indicator.telemetry import (
    Confidence,
    Source,
    Telemetry,
    TelemetryValidationError,
)
from ai_usage_indicator.telemetry_parsers import snapshot_from_grok_billing

OBSERVED = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def parse(fixture, name):
    return snapshot_from_grok_billing(fixture("grok", name), observed_at=OBSERVED)


def test_weekly_credits_and_optional_windows(fixture):
    tel = parse(fixture, "weekly_credits.json")
    assert tel.provider == "grok"
    assert tel.source is Source.OAUTH_API
    assert tel.confidence is Confidence.AUTHORITATIVE
    assert tel.plan == "SuperGrok"
    assert tel.observed_at == OBSERVED

    by_id = {w.id: w for w in tel.windows}
    assert set(by_id) == {"weekly", "product_grok_build", "on_demand"}

    weekly = by_id["weekly"]
    assert weekly.name == "Weekly"
    assert weekly.used_fraction == 0.425
    assert weekly.remaining_fraction == 0.575
    assert weekly.duration_seconds == 7 * 86400
    assert weekly.resets_at == datetime(2026, 6, 8, tzinfo=timezone.utc)

    build = by_id["product_grok_build"]
    assert build.name == "Build"
    assert build.used_fraction == 0.612

    extra = by_id["on_demand"]
    assert extra.name == "On-demand"
    assert extra.used_fraction == 0.06

    assert tel.most_constrained().id == "product_grok_build"


def test_omitted_proto3_percent_is_zero(fixture):
    tel = parse(fixture, "omitted_zero_percent.json")
    assert [w.id for w in tel.windows] == ["weekly"]
    assert tel.windows[0].used_fraction == 0.0
    assert tel.windows[0].resets_at == datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert tel.plan is None


def test_legacy_monthly_used_over_limit(fixture):
    tel = parse(fixture, "legacy_monthly.json")
    assert [w.id for w in tel.windows] == ["monthly"]
    monthly = tel.windows[0]
    assert monthly.name == "Monthly"
    assert monthly.used_fraction == 0.252573
    assert monthly.resets_at == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_round_trips_through_envelope(fixture):
    tel = parse(fixture, "weekly_credits.json")
    assert Telemetry.from_envelope(tel.to_envelope()) == tel


def test_invalid_percent_over_100_raises(fixture):
    with pytest.raises(TelemetryValidationError):
        parse(fixture, "invalid_percent_over_100.json")


def test_empty_payload_raises():
    with pytest.raises(TelemetryValidationError):
        snapshot_from_grok_billing({}, observed_at=OBSERVED)


def test_non_dict_payload_raises():
    with pytest.raises(TelemetryValidationError):
        snapshot_from_grok_billing([], observed_at=OBSERVED)
