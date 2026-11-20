"""GNOME-facing usage records derived from canonical telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from ai_usage_indicator.telemetry import QuotaWindow, Telemetry


class Pressure(Enum):
    """How close a provider is to its limit — drives the at-rest icon state."""

    NORMAL = "normal"
    WARNING = "warning"
    NEAR_LIMIT = "near-limit"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        """Higher rank = more urgent. Used to pick the worst provider for the tray."""
        return {"normal": 0, "unknown": 1, "warning": 2, "near-limit": 3}[self.value]


# Fraction thresholds for pressure levels.
WARNING_AT = 0.75
NEAR_LIMIT_AT = 0.90


@dataclass
class UsageRecord:
    """A single provider's usage, normalized so the tray can render any provider uniformly."""

    provider_id: str
    display_name: str
    used: float
    limit: float | None
    unit: str = ""
    reset_at: datetime | None = None
    # Optional human-readable summary; if omitted, one is derived from the fields above.
    label: str | None = None
    # Set when fetch failed so the menu can show the reason instead of stale numbers.
    error: str | None = None

    @property
    def fraction(self) -> float | None:
        """Used / limit in [0, 1], or None when the limit is unknown."""
        if self.limit is None or self.limit <= 0:
            return None
        return max(0.0, min(1.0, self.used / self.limit))

    @property
    def pressure(self) -> Pressure:
        if self.error is not None:
            return Pressure.UNKNOWN
        frac = self.fraction
        if frac is None:
            return Pressure.UNKNOWN
        if frac >= NEAR_LIMIT_AT:
            return Pressure.NEAR_LIMIT
        if frac >= WARNING_AT:
            return Pressure.WARNING
        return Pressure.NORMAL

    @property
    def percent(self) -> int | None:
        frac = self.fraction
        return None if frac is None else round(frac * 100)

    def glance_text(self) -> str:
        """Compact text for the at-rest tray label (e.g. '42%'). Empty if unknown."""
        pct = self.percent
        return "" if pct is None else f"{pct}%"

    def detail_text(self) -> str:
        """One-line description shown in the click-to-reveal menu."""
        if self.error is not None:
            return f"{self.display_name} — error: {self.error}"
        if self.label is not None:
            single_line = " · ".join(
                part.strip() for part in self.label.splitlines() if part.strip()
            )
            return f"{self.display_name} — {single_line}"
        pct = self.percent
        if pct is not None:
            return f"{self.display_name} — {pct}% used"
        unit = f" {self.unit}".rstrip()
        return f"{self.display_name} — {self.used:g}{unit} used"

    def reset_text(self) -> str | None:
        """Human-readable reset/renewal time, or None."""
        if self.reset_at is None:
            return None
        now = datetime.now(timezone.utc)
        delta = self.reset_at - now
        secs = delta.total_seconds()
        if secs <= 0:
            return "Resets: now"
        hours = secs / 3600
        if hours < 1:
            return f"Resets in {round(secs / 60)} min"
        if hours < 48:
            return f"Resets in {round(hours)} h"
        return f"Resets in {round(hours / 24)} d"


def _short_window_name(window: QuotaWindow) -> str:
    """Compact window name used by the existing GNOME state-file presentation."""
    if window.id == "five_hour" or window.name == "5-hour":
        return "5h"
    if window.id == "seven_day" or window.name in ("7-day", "Weekly"):
        return "wk"
    if window.id.startswith("seven_day_"):
        return window.id.removeprefix("seven_day_")
    return window.name.lower().replace("-hour", "h").replace("-day", "d")


def _format_windows_label(windows: list[QuotaWindow]) -> str:
    """Format quota windows compactly, grouping multi-window model families into rows.

    Single-family providers (Claude, Codex, Grok) stay on one line (``5h 0% · wk 46%``).
    Multi-family providers with multiple windows per family (e.g. Antigravity's Gemini and
    Claude 5-hour + weekly buckets) render one clean row per family::

        gemini: 5h 8% · wk 28%
        claude: 5h 0% · wk 0%
    """
    entries = [
        (_short_window_name(window), f"{round(window.used_fraction * 100)}%")
        for window in windows
    ]
    if entries and all(" " in name for name, _ in entries):
        grouped: dict[str, list[str]] = {}
        for name, pct in entries:
            family, detail = name.split(" ", 1)
            grouped.setdefault(family, []).append(f"{detail} {pct}")
        if len(grouped) >= 2 and any(len(parts) >= 2 for parts in grouped.values()):
            return "\n".join(
                f"{family}: {' · '.join(parts)}" for family, parts in grouped.items()
            )
    return " · ".join(f"{name} {pct}" for name, pct in entries)


def usage_from_telemetry(
    telemetry: Telemetry,
    *,
    display_name: str | None = None,
) -> UsageRecord:
    """Project full telemetry into the legacy single-headline GNOME model.

    The most-constrained window drives the percentage, pressure, and reset text while
    the label retains a compact breakdown of every reported window.
    """
    headline = telemetry.most_constrained()
    if headline is None:
        raise ValueError(f"{telemetry.provider} telemetry has no quota windows")
    return UsageRecord(
        provider_id=telemetry.provider,
        display_name=display_name or telemetry.provider.title(),
        used=headline.used_fraction * 100,
        limit=100.0,
        label=_format_windows_label(telemetry.windows),
        reset_at=headline.resets_at,
    )
