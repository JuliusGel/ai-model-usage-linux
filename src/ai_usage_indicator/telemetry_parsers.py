"""Pure parsers: raw provider responses -> :class:`~ai_usage_indicator.telemetry.Telemetry`.

These functions are the single place that understands each provider's on-the-wire shape and
turns it into the canonical, UI-agnostic telemetry model. They are deliberately kept separate
from the live ``Provider`` classes (``providers/claude.py``, ``providers/codex.py``), which
obtain raw payloads and then delegate all interpretation here.

Everything here is pure and read-only — inputs are already-decoded ``dict`` payloads (from a
live HTTP/JSON-RPC call or a recorded fixture), and ``observed_at`` is passed in rather than
read from the clock, so parsing is deterministic and testable.

Parsing is defensive: a missing optional field is tolerated, but malformed data (a fraction
out of range, a non-numeric reset time) raises
:class:`~ai_usage_indicator.telemetry.TelemetryValidationError` via the domain constructors.
"""

from __future__ import annotations

from datetime import datetime

from ai_usage_indicator.telemetry import (
    Confidence,
    QuotaWindow,
    Source,
    Telemetry,
    TelemetryValidationError,
    datetime_from_unix,
    parse_iso_datetime,
)

# --------------------------------------------------------------------------- Claude

# Fixed-length Claude windows and their stable identity. ``utilization`` is a 0-100 percentage.
_CLAUDE_FIVE_HOUR_SECONDS = 5 * 60 * 60
_CLAUDE_SEVEN_DAY_SECONDS = 7 * 24 * 60 * 60
_CLAUDE_WINDOWS = {
    "five_hour": ("5-hour", _CLAUDE_FIVE_HOUR_SECONDS),
    "seven_day": ("7-day", _CLAUDE_SEVEN_DAY_SECONDS),
}
# Prefix for optional model-specific weekly windows, e.g. ``seven_day_opus``. All share the
# 7-day duration; the trailing segment names the model.
_CLAUDE_MODEL_WEEKLY_PREFIX = "seven_day_"


def _percent_to_fraction(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TelemetryValidationError(f"{label} must be a number, got {value!r}")
    return float(value) / 100.0


def _claude_window(win_id: str, name: str, duration_seconds: int, raw: dict) -> QuotaWindow:
    used = _percent_to_fraction(raw.get("utilization") or 0.0, label=f"{win_id}.utilization")
    resets_raw = raw.get("resets_at")
    resets_at = None if resets_raw is None else parse_iso_datetime(resets_raw)
    return QuotaWindow.from_used_fraction(
        id=win_id,
        name=name,
        used_fraction=used,
        duration_seconds=duration_seconds,
        resets_at=resets_at,
    )


def snapshot_from_claude_usage(
    raw: dict,
    *,
    observed_at: datetime,
    source: Source = Source.OAUTH_API,
    provider: str = "claude",
) -> Telemetry:
    """Parse the Anthropic ``/api/oauth/usage`` payload into a snapshot.

    Preserves the ``five_hour`` and ``seven_day`` windows plus any optional model-specific
    weekly windows (``seven_day_<model>``), each with its own stable id.
    """
    if not isinstance(raw, dict):
        raise TelemetryValidationError(f"claude usage payload must be an object, got {raw!r}")

    windows: list[QuotaWindow] = []
    for win_id, (name, duration) in _CLAUDE_WINDOWS.items():
        block = raw.get(win_id)
        if isinstance(block, dict):
            windows.append(_claude_window(win_id, name, duration, block))

    # Optional model-specific weekly limits, e.g. "seven_day_opus".
    for key in sorted(raw):
        if key in _CLAUDE_WINDOWS or not key.startswith(_CLAUDE_MODEL_WEEKLY_PREFIX):
            continue
        block = raw.get(key)
        if not isinstance(block, dict):
            continue
        model = key[len(_CLAUDE_MODEL_WEEKLY_PREFIX):]
        windows.append(
            _claude_window(key, f"7-day ({model})", _CLAUDE_SEVEN_DAY_SECONDS, block)
        )

    if not windows:
        raise TelemetryValidationError("claude usage payload had no recognized windows")

    return Telemetry(
        provider=provider,
        observed_at=observed_at,
        source=source,
        confidence=Confidence.AUTHORITATIVE,
        windows=windows,
    )


# --------------------------------------------------------------------------- Codex

# Codex reports two positional buckets; the position is the stable id, the human name is
# derived from the window's duration.
_CODEX_BUCKETS = ("primary", "secondary")


def _codex_window_name(duration_seconds: float | None) -> str:
    if not duration_seconds:
        return "window"
    hours = duration_seconds / 3600
    days = duration_seconds / 86400
    if abs(days - 7) < 0.5:
        return "Weekly"
    if abs(hours - 5) < 0.5:
        return "5-hour"
    if hours < 48:
        return f"{round(hours)}-hour"
    return f"{round(days)}-day"


def _codex_window(bucket_id: str, raw: dict) -> QuotaWindow:
    if "usedPercent" not in raw:
        raise TelemetryValidationError(f"codex {bucket_id} window missing usedPercent")
    used = _percent_to_fraction(raw.get("usedPercent"), label=f"{bucket_id}.usedPercent")

    duration_mins = raw.get("windowDurationMins")
    duration_seconds: float | None = None
    if duration_mins is not None:
        if isinstance(duration_mins, bool) or not isinstance(duration_mins, (int, float)):
            raise TelemetryValidationError(
                f"codex {bucket_id}.windowDurationMins must be a number, got {duration_mins!r}"
            )
        duration_seconds = float(duration_mins) * 60.0

    resets_raw = raw.get("resetsAt")
    resets_at = None if resets_raw is None else datetime_from_unix(resets_raw)

    return QuotaWindow.from_used_fraction(
        id=bucket_id,
        name=_codex_window_name(duration_seconds),
        used_fraction=used,
        duration_seconds=duration_seconds,
        resets_at=resets_at,
    )


def snapshot_from_codex_rate_limits(
    raw: dict,
    *,
    observed_at: datetime,
    source: Source = Source.APP_SERVER,
    provider: str = "codex",
) -> Telemetry:
    """Parse the Codex app-server ``account/rateLimits/read`` response into a snapshot.

    Reads the backward-compatible single-bucket ``rateLimits`` view: ``planType`` plus the
    ``primary`` / ``secondary`` windows. A ``null`` bucket is skipped, so a weekly-only
    response (primary set, secondary null) yields a single window.
    """
    if not isinstance(raw, dict):
        raise TelemetryValidationError(f"codex payload must be an object, got {raw!r}")

    snapshot = raw.get("rateLimits")
    if not isinstance(snapshot, dict):
        raise TelemetryValidationError("codex payload missing 'rateLimits' object")

    windows: list[QuotaWindow] = []
    for bucket_id in _CODEX_BUCKETS:
        block = snapshot.get(bucket_id)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise TelemetryValidationError(
                f"codex {bucket_id} window must be an object or null, got {block!r}"
            )
        windows.append(_codex_window(bucket_id, block))

    if not windows:
        raise TelemetryValidationError("codex response had no rate-limit windows")

    plan = snapshot.get("planType")
    if plan is not None and not isinstance(plan, str):
        raise TelemetryValidationError(f"codex planType must be a string or null, got {plan!r}")

    return Telemetry(
        provider=provider,
        observed_at=observed_at,
        source=source,
        confidence=Confidence.AUTHORITATIVE,
        windows=windows,
        plan=plan,
    )
