"""Fixture-based tests for the Codex app-server rateLimits parser.

Fixtures mirror the ``account/rateLimits/read`` response shape (``GetAccountRateLimitsResponse``
-> ``RateLimitSnapshot``) emitted by Codex CLI 0.144.6, not the old direct backend endpoint.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_usage_indicator.telemetry import (
    Confidence,
    Source,
    Telemetry,
    TelemetryValidationError,
)
from ai_usage_indicator.telemetry_parsers import snapshot_from_codex_rate_limits

OBSERVED = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def parse(fixture, name):
    return snapshot_from_codex_rate_limits(fixture("codex", name), observed_at=OBSERVED)


def test_weekly_only_secondary_null(fixture):
    """Matches the live t1 evidence: planType plus, a 10080-min primary, secondary null."""
    tel = parse(fixture, "weekly_only.json")
    assert tel.provider == "codex"
    assert tel.source is Source.APP_SERVER
    assert tel.confidence is Confidence.AUTHORITATIVE
    assert tel.plan == "plus"

    assert [w.id for w in tel.windows] == ["primary"]
    win = tel.windows[0]
    assert win.name == "Weekly"
    assert win.used_fraction == 0.23
    assert win.remaining_fraction == 0.77
    assert win.duration_seconds == 10080 * 60
    assert win.duration.days == 7
    assert win.resets_at == datetime.fromtimestamp(1753660800, tz=timezone.utc)


def test_primary_and_secondary(fixture):
    tel = parse(fixture, "primary_and_secondary.json")
    assert tel.plan == "pro"
    by_id = {w.id: w for w in tel.windows}
    assert set(by_id) == {"primary", "secondary"}

    assert by_id["primary"].name == "5-hour"
    assert by_id["primary"].used_fraction == 0.40
    assert by_id["secondary"].name == "Weekly"
    assert by_id["secondary"].used_fraction == 0.72
    # secondary (weekly) is more constrained than primary (5h).
    assert tel.most_constrained().id == "secondary"


def test_round_trips_through_envelope(fixture):
    tel = parse(fixture, "weekly_only.json")
    assert Telemetry.from_envelope(tel.to_envelope()) == tel


def test_invalid_used_percent_over_100_raises(fixture):
    with pytest.raises(TelemetryValidationError):
        parse(fixture, "invalid_used_percent_over_100.json")


def test_invalid_reset_not_numeric_raises(fixture):
    with pytest.raises(TelemetryValidationError):
        parse(fixture, "invalid_reset_not_numeric.json")


def test_missing_rate_limits_raises():
    with pytest.raises(TelemetryValidationError):
        snapshot_from_codex_rate_limits({}, observed_at=OBSERVED)


def test_all_buckets_null_raises():
    payload = {"rateLimits": {"planType": "plus", "primary": None, "secondary": None}}
    with pytest.raises(TelemetryValidationError):
        snapshot_from_codex_rate_limits(payload, observed_at=OBSERVED)


def test_window_missing_used_percent_raises():
    payload = {"rateLimits": {"primary": {"windowDurationMins": 10080}}}
    with pytest.raises(TelemetryValidationError):
        snapshot_from_codex_rate_limits(payload, observed_at=OBSERVED)


def test_window_without_duration_gets_generic_name():
    payload = {"rateLimits": {"primary": {"usedPercent": 10}}}
    tel = snapshot_from_codex_rate_limits(payload, observed_at=OBSERVED)
    win = tel.windows[0]
    assert win.name == "window"
    assert win.duration_seconds is None
    assert win.resets_at is None
