"""Tests for the telemetry domain model, JSON round-trip, and envelope versioning."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ai_usage_indicator.telemetry import (
    SCHEMA_VERSION,
    Confidence,
    QuotaWindow,
    SchemaVersionError,
    Source,
    Telemetry,
    TelemetryValidationError,
    datetime_from_unix,
    parse_iso_datetime,
)

OBSERVED = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def make_window(**overrides) -> QuotaWindow:
    kwargs = dict(
        id="five_hour",
        name="5-hour",
        used_fraction=0.45,
        remaining_fraction=0.55,
        duration_seconds=5 * 3600,
        resets_at=datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc),
    )
    kwargs.update(overrides)
    return QuotaWindow(**kwargs)


def make_telemetry(**overrides) -> Telemetry:
    kwargs = dict(
        provider="claude",
        observed_at=OBSERVED,
        source=Source.OAUTH_API,
        confidence=Confidence.AUTHORITATIVE,
        windows=[
            make_window(),
            make_window(
                id="seven_day",
                name="7-day",
                used_fraction=0.30,
                remaining_fraction=0.70,
                duration_seconds=7 * 86400,
                resets_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        ],
    )
    kwargs.update(overrides)
    return Telemetry(**kwargs)


# --------------------------------------------------------------------------- QuotaWindow


def test_from_used_fraction_derives_remaining():
    win = QuotaWindow.from_used_fraction(id="w", name="W", used_fraction=0.25)
    assert win.used_fraction == 0.25
    assert win.remaining_fraction == 0.75


def test_duration_property_returns_timedelta():
    assert make_window().duration == timedelta(hours=5)
    assert make_window(duration_seconds=None).duration is None


def test_resets_at_normalized_to_utc():
    tz = timezone(timedelta(hours=2))
    win = make_window(resets_at=datetime(2026, 7, 23, 22, 0, tzinfo=tz))
    assert win.resets_at.tzinfo == timezone.utc
    assert win.resets_at == datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("used", [-0.01, 1.5, 2.0])
def test_used_fraction_out_of_range_rejected(used):
    with pytest.raises(TelemetryValidationError):
        make_window(used_fraction=used, remaining_fraction=0.0)


def test_inconsistent_fraction_pair_rejected():
    with pytest.raises(TelemetryValidationError):
        make_window(used_fraction=0.4, remaining_fraction=0.4)


@pytest.mark.parametrize("bad", [True, "0.5", None])
def test_non_numeric_fraction_rejected(bad):
    with pytest.raises(TelemetryValidationError):
        make_window(used_fraction=bad)


def test_negative_duration_rejected():
    with pytest.raises(TelemetryValidationError):
        make_window(duration_seconds=-1)


def test_naive_reset_rejected():
    with pytest.raises(TelemetryValidationError):
        make_window(resets_at=datetime(2026, 7, 23, 20, 0))


def test_empty_id_or_name_rejected():
    with pytest.raises(TelemetryValidationError):
        make_window(id="")
    with pytest.raises(TelemetryValidationError):
        make_window(name="")


# --------------------------------------------------------------------------- Telemetry


def test_duplicate_window_ids_rejected():
    with pytest.raises(TelemetryValidationError):
        make_telemetry(windows=[make_window(), make_window()])


def test_observed_at_must_be_aware():
    with pytest.raises(TelemetryValidationError):
        make_telemetry(observed_at=datetime(2026, 7, 23, 12, 0))


def test_most_constrained_picks_highest_used():
    tel = make_telemetry()
    assert tel.most_constrained().id == "five_hour"  # 0.45 > 0.30


def test_most_constrained_none_when_empty():
    assert make_telemetry(windows=[]).most_constrained() is None


def test_most_constrained_tie_breaks_on_sooner_reset():
    soon = datetime(2026, 7, 24, tzinfo=timezone.utc)
    late = datetime(2026, 7, 30, tzinfo=timezone.utc)
    tel = make_telemetry(
        windows=[
            make_window(id="a", used_fraction=0.5, remaining_fraction=0.5, resets_at=late),
            make_window(id="b", used_fraction=0.5, remaining_fraction=0.5, resets_at=soon),
        ]
    )
    assert tel.most_constrained().id == "b"


def test_source_and_confidence_accept_raw_strings():
    tel = make_telemetry(source="oauth_api", confidence="authoritative")
    assert tel.source is Source.OAUTH_API
    assert tel.confidence is Confidence.AUTHORITATIVE


def test_invalid_source_rejected():
    with pytest.raises(ValueError):
        make_telemetry(source="carrier_pigeon")


# --------------------------------------------------------------------------- round-trip


def test_dict_round_trip_preserves_all_windows():
    tel = make_telemetry(plan="pro")
    restored = Telemetry.from_dict(tel.to_dict())
    assert restored == tel
    assert [w.id for w in restored.windows] == ["five_hour", "seven_day"]


def test_json_envelope_round_trip():
    tel = make_telemetry(plan="plus")
    blob = json.dumps(tel.to_envelope())
    restored = Telemetry.from_envelope(json.loads(blob))
    assert restored == tel


def test_envelope_carries_schema_version():
    assert make_telemetry().to_envelope()["schema_version"] == SCHEMA_VERSION


def test_window_none_reset_and_duration_survive_round_trip():
    tel = make_telemetry(
        windows=[make_window(duration_seconds=None, resets_at=None)]
    )
    restored = Telemetry.from_dict(tel.to_dict())
    assert restored.windows[0].duration_seconds is None
    assert restored.windows[0].resets_at is None


# --------------------------------------------------------------------------- versioning


def test_wrong_schema_version_rejected():
    env = make_telemetry().to_envelope()
    env["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(SchemaVersionError) as exc:
        Telemetry.from_envelope(env)
    assert exc.value.got == SCHEMA_VERSION + 1
    assert exc.value.expected == SCHEMA_VERSION


def test_missing_schema_version_rejected():
    with pytest.raises(SchemaVersionError):
        Telemetry.from_envelope({"telemetry": make_telemetry().to_dict()})


def test_envelope_missing_payload_rejected():
    with pytest.raises(TelemetryValidationError):
        Telemetry.from_envelope({"schema_version": SCHEMA_VERSION})


# --------------------------------------------------------------------------- helpers


def test_parse_iso_datetime_accepts_z_suffix():
    assert parse_iso_datetime("2026-07-23T20:00:00Z") == datetime(
        2026, 7, 23, 20, 0, tzinfo=timezone.utc
    )


def test_parse_iso_datetime_rejects_naive():
    with pytest.raises(TelemetryValidationError):
        parse_iso_datetime("2026-07-23T20:00:00")


@pytest.mark.parametrize("bad", ["", "not-a-date", 12345, None])
def test_parse_iso_datetime_rejects_junk(bad):
    with pytest.raises(TelemetryValidationError):
        parse_iso_datetime(bad)


def test_datetime_from_unix():
    assert datetime_from_unix(0) == datetime(1970, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", ["soon", True, None])
def test_datetime_from_unix_rejects_junk(bad):
    with pytest.raises(TelemetryValidationError):
        datetime_from_unix(bad)
