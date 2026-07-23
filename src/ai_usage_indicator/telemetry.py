"""Versioned, UI-agnostic full-window usage telemetry.

This is the canonical domain model for a provider's plan usage. Unlike ``UsageRecord``
(``usage.py``), which is a UI-oriented *headline* summary the GNOME tray renders, this
model preserves **every** quota window a provider reports so that downstream consumers —
the GNOME indicator *and* an OpenClaw budgeter — can each pick what they need from the same
data. t3 will refactor the providers to emit these snapshots; t2 defines the schema and its
tests only.

Design goals:

* **Full fidelity.** Keep all windows (5-hour, 7-day, model-specific weekly, primary /
  secondary rate-limit buckets, ...), not just the most-constrained one.
* **Provider-neutral.** Windows carry a stable machine ``id`` and a human ``name``; consumers
  never parse provider-specific raw shapes.
* **Self-describing + versioned.** A JSON envelope carries ``schema_version`` so an
  incompatible reader can detect and reject payloads it does not understand.
* **Defensive.** Fractions and reset metadata are validated on construction; malformed data
  raises :class:`TelemetryValidationError` rather than silently producing a bad snapshot.
* **Pure stdlib, read-only.** No network, no credentials, no third-party deps.

The Python dataclasses and their JSON representation are kept in lock-step by
:meth:`Telemetry.to_dict` / :meth:`Telemetry.from_dict` (round-trip tested).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

#: Envelope schema version. Bump on any *incompatible* change to the JSON shape below so
#: older readers can reject payloads they cannot parse. Additive, optional fields do not
#: require a bump; renaming/removing fields or changing their meaning does.
SCHEMA_VERSION = 1

# Fractions are stored rounded to this many decimals so JSON round-trips are exact and
# value-equality is stable (float noise from deriving ``remaining = 1 - used`` cannot leak
# into equality comparisons).
_FRACTION_DECIMALS = 6
# How far ``used_fraction + remaining_fraction`` may stray from 1.0 before we treat the pair
# as contradictory. Generous enough to absorb rounding, tight enough to catch real bugs.
_CONSISTENCY_TOLERANCE = 1e-3


class TelemetryError(Exception):
    """Base class for telemetry schema errors."""


class TelemetryValidationError(TelemetryError, ValueError):
    """A telemetry value failed validation (bad fraction, bad reset metadata, ...)."""


class SchemaVersionError(TelemetryError):
    """A JSON envelope carried a ``schema_version`` this reader cannot handle."""

    def __init__(self, got: object, expected: int = SCHEMA_VERSION) -> None:
        super().__init__(
            f"unsupported telemetry schema_version {got!r} (this reader supports {expected})"
        )
        self.got = got
        self.expected = expected


class Source(str, Enum):
    """Where a snapshot's data physically came from."""

    OAUTH_API = "oauth_api"          # provider's account/usage HTTP endpoint (Claude)
    APP_SERVER = "app_server"        # a local CLI's app-server / JSON-RPC bridge (Codex)
    CACHE = "cache"                  # a previously observed snapshot replayed from disk
    FIXTURE = "fixture"              # a recorded fixture (tests / offline)
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    """How authoritative the numbers are."""

    AUTHORITATIVE = "authoritative"  # reported directly by the provider
    DERIVED = "derived"              # computed/estimated from partial data
    STALE = "stale"                  # last-known values, possibly out of date
    UNKNOWN = "unknown"


def parse_iso_datetime(value: object) -> datetime:
    """Parse an ISO-8601 string into a timezone-aware UTC-normalized datetime.

    A trailing ``Z`` is accepted. A naive datetime string is rejected — we refuse to guess a
    timezone for reset metadata. Raises :class:`TelemetryValidationError` on anything that is
    not a clean, tz-aware ISO-8601 string.
    """
    if not isinstance(value, str) or not value.strip():
        raise TelemetryValidationError(f"expected ISO-8601 datetime string, got {value!r}")
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TelemetryValidationError(f"invalid ISO-8601 datetime {value!r}: {exc}") from exc
    if dt.tzinfo is None:
        raise TelemetryValidationError(f"datetime {value!r} is missing a timezone offset")
    return dt.astimezone(timezone.utc)


def datetime_from_unix(value: object) -> datetime:
    """Parse a Unix timestamp (seconds) into a tz-aware UTC datetime.

    Accepts int/float seconds. Booleans and non-numerics are rejected (``bool`` is a subclass
    of ``int`` in Python, and a boolean reset time is always a bug).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TelemetryValidationError(f"expected Unix seconds, got {value!r}")
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise TelemetryValidationError(f"invalid Unix timestamp {value!r}: {exc}") from exc


def _clean_fraction(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TelemetryValidationError(f"{label} must be a number, got {value!r}")
    frac = round(float(value), _FRACTION_DECIMALS)
    if frac < 0.0 or frac > 1.0:
        raise TelemetryValidationError(f"{label} {value!r} is out of range [0, 1]")
    return frac


@dataclass
class QuotaWindow:
    """One rate-limit / quota window of a provider's plan.

    ``id`` is a stable machine key (``"five_hour"``, ``"primary"``); ``name`` is a human label
    (``"5-hour"``, ``"Weekly"``). ``used_fraction`` and ``remaining_fraction`` are in ``[0, 1]``
    and, when both are meaningful, sum to ~1.0. ``duration_seconds`` is the window length when
    known; ``resets_at`` is when the window rolls over (tz-aware UTC) when known.
    """

    id: str
    name: str
    used_fraction: float
    remaining_fraction: float
    duration_seconds: float | None = None
    resets_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise TelemetryValidationError(f"window id must be a non-empty string, got {self.id!r}")
        if not self.name or not isinstance(self.name, str):
            raise TelemetryValidationError(
                f"window name must be a non-empty string, got {self.name!r}"
            )
        self.used_fraction = _clean_fraction(self.used_fraction, label="used_fraction")
        self.remaining_fraction = _clean_fraction(
            self.remaining_fraction, label="remaining_fraction"
        )
        drift = abs(self.used_fraction + self.remaining_fraction - 1.0)
        if drift > _CONSISTENCY_TOLERANCE:
            raise TelemetryValidationError(
                f"used_fraction ({self.used_fraction}) + remaining_fraction "
                f"({self.remaining_fraction}) = {self.used_fraction + self.remaining_fraction}, "
                "which is not ~1.0"
            )
        if self.duration_seconds is not None:
            if isinstance(self.duration_seconds, bool) or not isinstance(
                self.duration_seconds, (int, float)
            ):
                raise TelemetryValidationError(
                    f"duration_seconds must be a number or None, got {self.duration_seconds!r}"
                )
            if self.duration_seconds <= 0:
                raise TelemetryValidationError(
                    f"duration_seconds must be positive, got {self.duration_seconds!r}"
                )
            self.duration_seconds = float(self.duration_seconds)
        if self.resets_at is not None:
            if not isinstance(self.resets_at, datetime):
                raise TelemetryValidationError(
                    f"resets_at must be a datetime or None, got {self.resets_at!r}"
                )
            if self.resets_at.tzinfo is None:
                raise TelemetryValidationError("resets_at must be timezone-aware")
            self.resets_at = self.resets_at.astimezone(timezone.utc)

    @classmethod
    def from_used_fraction(
        cls,
        *,
        id: str,
        name: str,
        used_fraction: float,
        duration_seconds: float | None = None,
        resets_at: datetime | None = None,
    ) -> QuotaWindow:
        """Build a window from ``used_fraction`` alone, deriving ``remaining_fraction``.

        Both Claude (``utilization``) and Codex (``usedPercent``) report *used* only; the
        remaining fraction is the natural complement. Validation of the derived value happens
        in ``__post_init__`` so an out-of-range ``used_fraction`` still fails loudly.
        """
        used = _clean_fraction(used_fraction, label="used_fraction")
        return cls(
            id=id,
            name=name,
            used_fraction=used,
            remaining_fraction=round(1.0 - used, _FRACTION_DECIMALS),
            duration_seconds=duration_seconds,
            resets_at=resets_at,
        )

    @property
    def duration(self) -> timedelta | None:
        """Window length as a :class:`~datetime.timedelta`, or ``None`` if unknown."""
        return None if self.duration_seconds is None else timedelta(seconds=self.duration_seconds)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "used_fraction": self.used_fraction,
            "remaining_fraction": self.remaining_fraction,
            "duration_seconds": self.duration_seconds,
            "resets_at": None if self.resets_at is None else self.resets_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> QuotaWindow:
        if not isinstance(data, dict):
            raise TelemetryValidationError(f"window must be an object, got {data!r}")
        resets_raw = data.get("resets_at")
        return cls(
            id=data.get("id"),
            name=data.get("name"),
            used_fraction=data.get("used_fraction"),
            remaining_fraction=data.get("remaining_fraction"),
            duration_seconds=data.get("duration_seconds"),
            resets_at=None if resets_raw is None else parse_iso_datetime(resets_raw),
        )


@dataclass
class Telemetry:
    """A full-fidelity snapshot of one provider's plan usage at a point in time."""

    provider: str
    observed_at: datetime
    source: Source
    confidence: Confidence
    windows: list[QuotaWindow] = field(default_factory=list)
    #: Optional plan identifier a provider may surface (Codex ``planType``); ``None`` if unknown.
    plan: str | None = None

    def __post_init__(self) -> None:
        if not self.provider or not isinstance(self.provider, str):
            raise TelemetryValidationError(
                f"provider must be a non-empty string, got {self.provider!r}"
            )
        if not isinstance(self.observed_at, datetime):
            raise TelemetryValidationError(
                f"observed_at must be a datetime, got {self.observed_at!r}"
            )
        if self.observed_at.tzinfo is None:
            raise TelemetryValidationError("observed_at must be timezone-aware")
        self.observed_at = self.observed_at.astimezone(timezone.utc)
        self.source = Source(self.source)
        self.confidence = Confidence(self.confidence)
        ids = [w.id for w in self.windows]
        if len(ids) != len(set(ids)):
            raise TelemetryValidationError(f"duplicate window ids in snapshot: {ids}")

    def most_constrained(self) -> QuotaWindow | None:
        """The window closest to its limit — the natural headline for a tray/budgeter.

        Ties break toward the sooner reset. Returns ``None`` when there are no windows.
        """
        if not self.windows:
            return None
        _far_future = datetime.max.replace(tzinfo=timezone.utc)
        return max(
            self.windows,
            key=lambda w: (w.used_fraction, -(w.resets_at or _far_future).timestamp()),
        )

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "observed_at": self.observed_at.isoformat(),
            "source": self.source.value,
            "confidence": self.confidence.value,
            "plan": self.plan,
            "windows": [w.to_dict() for w in self.windows],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Telemetry:
        if not isinstance(data, dict):
            raise TelemetryValidationError(f"telemetry must be an object, got {data!r}")
        windows_raw = data.get("windows") or []
        if not isinstance(windows_raw, list):
            raise TelemetryValidationError(f"windows must be a list, got {windows_raw!r}")
        return cls(
            provider=data.get("provider"),
            observed_at=parse_iso_datetime(data.get("observed_at")),
            source=data.get("source"),
            confidence=data.get("confidence"),
            windows=[QuotaWindow.from_dict(w) for w in windows_raw],
            plan=data.get("plan"),
        )

    def to_envelope(self) -> dict:
        """Wrap this snapshot in the versioned JSON envelope."""
        return {"schema_version": SCHEMA_VERSION, "telemetry": self.to_dict()}

    @classmethod
    def from_envelope(cls, envelope: dict) -> Telemetry:
        """Unwrap and validate a versioned envelope.

        Raises :class:`SchemaVersionError` if ``schema_version`` is missing or unsupported,
        so an incompatible payload is rejected rather than silently mis-parsed.
        """
        if not isinstance(envelope, dict):
            raise TelemetryValidationError(f"envelope must be an object, got {envelope!r}")
        version = envelope.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(version)
        payload = envelope.get("telemetry")
        if not isinstance(payload, dict):
            raise TelemetryValidationError("envelope is missing a 'telemetry' object")
        return cls.from_dict(payload)
