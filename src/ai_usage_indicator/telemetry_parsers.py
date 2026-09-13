"""Pure parsers: raw provider responses -> :class:`~ai_usage_indicator.telemetry.Telemetry`.

These functions are the single place that understands each provider's on-the-wire shape and
turns it into the canonical, UI-agnostic telemetry model. They are deliberately kept separate
from the live ``Provider`` classes (``providers/claude.py``, ``providers/codex.py``,
``providers/grok.py``), which obtain raw payloads and then delegate all interpretation here.

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


# --------------------------------------------------------------------------- Grok

_GROK_WEEKLY_SECONDS = 7 * 24 * 60 * 60
_GROK_MONTHLY_SECONDS = 30 * 24 * 60 * 60
_GROK_PRODUCT_SHORT = {
    "PRODUCT_GROK_BUILD": "Build",
    "PRODUCT_GROK": "Chat",
}


def _cent(obj: dict, key: str) -> int | None:
    """Read a proto3 `{val: N}` object. A present object with no val is 0."""
    raw = obj.get(key)
    if raw is None:
        return None
    if isinstance(raw, dict):
        if "val" not in raw:
            return 0
        if isinstance(raw["val"], bool) or not isinstance(raw["val"], (int, float)):
            raise TelemetryValidationError(f"grok {key}.val must be a number, got {raw['val']!r}")
        return int(raw["val"])
    raise TelemetryValidationError(f"grok {key} must be an object or omitted, got {raw!r}")


def _grok_period_meta(cfg: dict) -> tuple[str, str, float | None, datetime | None]:
    """Return (window_id, name, duration_seconds, resets_at) for the current billing period."""
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    ptype = str(period.get("type") or "").upper()
    start_raw = period.get("start") or cfg.get("billingPeriodStart")
    end_raw = period.get("end") or cfg.get("billingPeriodEnd")
    start = None if start_raw is None else parse_iso_datetime(start_raw)
    end = None if end_raw is None else parse_iso_datetime(end_raw)

    if "WEEKLY" in ptype:
        win_id, name, duration = "weekly", "Weekly", float(_GROK_WEEKLY_SECONDS)
    elif "MONTHLY" in ptype:
        win_id, name, duration = "monthly", "Monthly", float(_GROK_MONTHLY_SECONDS)
    elif start and end:
        days = (end - start).total_seconds() / 86400
        if 4 <= days <= 12:
            win_id, name, duration = "weekly", "Weekly", float(_GROK_WEEKLY_SECONDS)
        elif 20 <= days <= 45:
            win_id, name, duration = "monthly", "Monthly", float(_GROK_MONTHLY_SECONDS)
        else:
            win_id, name, duration = "current", "Window", None
    else:
        win_id, name, duration = "current", "Window", None

    if start and end:
        span = (end - start).total_seconds()
        if span > 0:
            duration = span
    return win_id, name, duration, end


def snapshot_from_grok_billing(
    raw: dict,
    *,
    observed_at: datetime,
    source: Source = Source.OAUTH_API,
    provider: str = "grok",
) -> Telemetry:
    """Parse ``GET /v1/billing?format=credits`` into a snapshot.

    Prefers ``config.creditUsagePercent`` (0–100). proto3 omits zero scalars, so a current
    period with no percent is treated as 0%. Falls back to the legacy
    ``used.val / monthlyLimit.val`` shape. Optional ``productUsage`` and on-demand cap
    windows are preserved when present.
    """
    if not isinstance(raw, dict):
        raise TelemetryValidationError(f"grok billing payload must be an object, got {raw!r}")

    cfg = raw.get("config")
    if not isinstance(cfg, dict):
        raise TelemetryValidationError("grok billing payload missing 'config' object")

    used_fraction: float | None = None
    raw_pct = cfg.get("creditUsagePercent")
    if raw_pct is not None:
        used_fraction = _percent_to_fraction(raw_pct, label="creditUsagePercent")
    else:
        used_val = _cent(cfg, "used")
        limit_val = _cent(cfg, "monthlyLimit")
        if used_val is not None and limit_val is not None:
            if limit_val <= 0:
                raise TelemetryValidationError(
                    f"grok monthlyLimit.val must be positive, got {limit_val!r}"
                )
            used_fraction = _percent_to_fraction(
                100.0 * used_val / limit_val, label="used/monthlyLimit"
            )
        elif cfg.get("currentPeriod") or cfg.get("billingPeriodEnd"):
            used_fraction = 0.0
        else:
            raise TelemetryValidationError("grok billing payload had no usage data")
    win_id, name, duration, resets_at = _grok_period_meta(cfg)
    windows = [
        QuotaWindow.from_used_fraction(
            id=win_id,
            name=name,
            used_fraction=used_fraction,
            duration_seconds=duration,
            resets_at=resets_at,
        )
    ]

    for product in cfg.get("productUsage") or []:
        if not isinstance(product, dict):
            continue
        pct = product.get("usagePercent")
        if pct is None:
            continue
        raw_name = str(product.get("product") or "product")
        short = _GROK_PRODUCT_SHORT.get(raw_name, raw_name.replace("PRODUCT_", "").title() or "Product")
        prod_id = "product_" + raw_name.replace("PRODUCT_", "").lower()
        windows.append(
            QuotaWindow.from_used_fraction(
                id=prod_id or "product",
                name=short,
                used_fraction=_percent_to_fraction(pct, label=f"{raw_name}.usagePercent"),
            )
        )

    od_cap = _cent(cfg, "onDemandCap") or 0
    if od_cap > 0:
        od_used = _cent(cfg, "onDemandUsed") or 0
        windows.append(
            QuotaWindow.from_used_fraction(
                id="on_demand",
                name="On-demand",
                used_fraction=_percent_to_fraction(
                    100.0 * od_used / od_cap, label="onDemandUsed"
                ),
            )
        )

    plan = raw.get("subscriptionTier")
    if plan is not None and not isinstance(plan, str):
        raise TelemetryValidationError(
            f"grok subscriptionTier must be a string or null, got {plan!r}"
        )

    return Telemetry(
        provider=provider,
        observed_at=observed_at,
        source=source,
        confidence=Confidence.AUTHORITATIVE,
        windows=windows,
        plan=plan,
    )


# ------------------------------------------------------------- xAI console (Management API)

# The Grok CLI's own billing endpoint reports no plan percent for Team principals — it answers
# with a bare billing period and zeroed counters. Real spend for those accounts lives behind
# the xAI Management API (the same data console.x.ai renders), which needs a *management key*
# rather than the CLI's OAuth token. These parsers read those two payloads.

#: Management API money fields are whole cents, usually JSON-encoded as decimal strings.
_CENTS_PER_USD = 100.0


def _usd_from_cents(value: object, *, label: str) -> float:
    """Read a Management API money field (``"25351"`` or ``{"val": "25351"}``) as USD."""
    if isinstance(value, dict):
        value = value.get("val")
    if isinstance(value, bool) or value is None:
        raise TelemetryValidationError(f"{label} must be a money value, got {value!r}")
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError as exc:
            raise TelemetryValidationError(f"{label} {value!r} is not an integer: {exc}") from exc
    if not isinstance(value, (int, float)):
        raise TelemetryValidationError(f"{label} must be a number, got {value!r}")
    return float(value) / _CENTS_PER_USD


def xai_console_spend_usd(raw: dict) -> float:
    """Total USD in a ``POST /v1/billing/teams/{id}/usage`` analytics response.

    The response is a set of time series, each a list of ``dataPoints`` whose ``values`` line
    up with the requested ``values`` names. We request ``usd`` alone, so summing every point
    of every series yields the window total — the figure console.x.ai's Usage Explorer shows.
    Unlike the rest of the Management API these are already USD floats, not cents.
    """
    if not isinstance(raw, dict):
        raise TelemetryValidationError(f"xai usage payload must be an object, got {raw!r}")
    series = raw.get("timeSeries")
    if not isinstance(series, list):
        raise TelemetryValidationError("xai usage payload missing 'timeSeries' list")

    total = 0.0
    for entry in series:
        if not isinstance(entry, dict):
            raise TelemetryValidationError(f"xai time series must be an object, got {entry!r}")
        for point in entry.get("dataPoints") or []:
            if not isinstance(point, dict):
                raise TelemetryValidationError(f"xai data point must be an object, got {point!r}")
            for value in point.get("values") or []:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TelemetryValidationError(
                        f"xai usage value must be a number, got {value!r}"
                    )
                total += float(value)
    return total


def xai_console_credits_usd(raw: dict) -> float | None:
    """Remaining credit balance from ``GET /v1/billing/teams/{id}/postpaid/invoice/preview``.

    ``defaultCredits`` is the console's credit total for the team. ``None`` when the field is
    absent, so the caller can fall back to a configured allowance rather than guess.
    """
    if not isinstance(raw, dict):
        raise TelemetryValidationError(f"xai invoice payload must be an object, got {raw!r}")
    credits = raw.get("defaultCredits")
    if credits is None:
        return None
    return _usd_from_cents(credits, label="defaultCredits")


def snapshot_from_xai_console(
    *,
    used_usd: float,
    credits_usd: float,
    period_start: datetime | None,
    period_end: datetime | None,
    observed_at: datetime,
    provider: str = "grok",
) -> Telemetry:
    """Build a snapshot from console spend measured against the team's credit total."""
    if isinstance(credits_usd, bool) or not isinstance(credits_usd, (int, float)):
        raise TelemetryValidationError(f"credits_usd must be a number, got {credits_usd!r}")
    if credits_usd <= 0:
        raise TelemetryValidationError(f"credits_usd must be positive, got {credits_usd!r}")
    if isinstance(used_usd, bool) or not isinstance(used_usd, (int, float)):
        raise TelemetryValidationError(f"used_usd must be a number, got {used_usd!r}")

    duration: float | None = None
    if period_start is not None and period_end is not None:
        span = (period_end - period_start).total_seconds()
        duration = span if span > 0 else None

    return Telemetry(
        provider=provider,
        observed_at=observed_at,
        source=Source.MANAGEMENT_API,
        confidence=Confidence.AUTHORITATIVE,
        windows=[
            QuotaWindow.from_used_fraction(
                id="weekly" if duration and 4 * 86400 <= duration <= 12 * 86400 else "current",
                name="Weekly" if duration and 4 * 86400 <= duration <= 12 * 86400 else "Window",
                # Clamped: spend can exceed the credit balance once billing takes over.
                used_fraction=min(1.0, max(0.0, float(used_usd) / float(credits_usd))),
                duration_seconds=duration,
                resets_at=period_end,
            )
        ],
    )
