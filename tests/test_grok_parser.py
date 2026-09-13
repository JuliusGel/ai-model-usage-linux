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
from ai_usage_indicator.telemetry_parsers import (
    snapshot_from_grok_billing,
    snapshot_from_xai_console,
    xai_console_credits_usd,
    xai_console_spend_usd,
)

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


# --------------------------------------------------------- xAI console (Management API)

PERIOD_START = datetime(2026, 9, 8, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 9, 15, tzinfo=timezone.utc)


def test_console_spend_sums_every_data_point(fixture):
    assert xai_console_spend_usd(fixture("grok", "console_usage.json")) == pytest.approx(
        8.1138438612
    )


def test_console_spend_of_empty_series_is_zero():
    assert xai_console_spend_usd({"timeSeries": []}) == 0.0


def test_console_spend_without_time_series_raises():
    with pytest.raises(TelemetryValidationError):
        xai_console_spend_usd({"limitReached": False})


def test_console_spend_rejects_non_numeric_value():
    with pytest.raises(TelemetryValidationError):
        xai_console_spend_usd({"timeSeries": [{"dataPoints": [{"values": ["8.11"]}]}]})


def test_console_credits_are_cents(fixture):
    credits = xai_console_credits_usd(fixture("grok", "console_invoice_preview.json"))
    assert credits == pytest.approx(253.51)


def test_console_credits_absent_is_none():
    assert xai_console_credits_usd({"billingCycle": {"year": 2026, "month": 9}}) is None


def test_console_snapshot_measures_spend_against_credits():
    tel = snapshot_from_xai_console(
        used_usd=8.1138438612,
        credits_usd=253.51,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        observed_at=OBSERVED,
        provider="xai",
    )
    assert tel.source is Source.MANAGEMENT_API
    assert tel.confidence is Confidence.AUTHORITATIVE
    (window,) = tel.windows
    assert window.id == "weekly"
    assert window.used_fraction == pytest.approx(0.032006, abs=1e-6)
    assert window.duration_seconds == 7 * 86400
    assert window.resets_at == PERIOD_END


def test_console_snapshot_clamps_overspend():
    tel = snapshot_from_xai_console(
        used_usd=400.0,
        credits_usd=253.51,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        observed_at=OBSERVED,
    )
    assert tel.windows[0].used_fraction == 1.0


def test_console_snapshot_rejects_zero_credits():
    with pytest.raises(TelemetryValidationError):
        snapshot_from_xai_console(
            used_usd=1.0,
            credits_usd=0,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            observed_at=OBSERVED,
        )


def test_console_snapshot_without_period_has_no_duration():
    tel = snapshot_from_xai_console(
        used_usd=1.0,
        credits_usd=10.0,
        period_start=None,
        period_end=None,
        observed_at=OBSERVED,
    )
    (window,) = tel.windows
    assert window.id == "current"
    assert window.duration_seconds is None
    assert window.resets_at is None
