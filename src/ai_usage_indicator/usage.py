"""Normalized usage records shared across all providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


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
            return f"{self.display_name} — {self.label}"
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
