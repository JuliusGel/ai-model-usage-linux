"""Read-only provider interface and failure-isolating adapters."""

from __future__ import annotations

import abc
from dataclasses import dataclass

from ai_usage_indicator.telemetry import Telemetry
from ai_usage_indicator.usage import UsageRecord
from ai_usage_indicator.usage import usage_from_telemetry


class ProviderError(RuntimeError):
    """A provider could not produce valid telemetry."""


@dataclass(frozen=True)
class TelemetryFetch:
    """Failure-isolated result of one provider read."""

    provider_id: str
    display_name: str
    telemetry: Telemetry | None = None
    error: str | None = None


class Provider(abc.ABC):
    """A source of AI-subscription usage.

    Implementations emit canonical full-window :class:`Telemetry`. Authentication remains
    owned by the official CLIs: adapters may read an existing token in memory, but never
    persist, copy, or refresh credentials.
    """

    #: Stable machine id used in config (e.g. "claude").
    id: str
    #: Human-facing name shown in the menu (e.g. "Claude").
    display_name: str

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    @abc.abstractmethod
    def authenticate(self) -> None:
        """Prepare a read-only session. May be a no-op."""

    @abc.abstractmethod
    def fetch_telemetry(self) -> Telemetry:
        """Read and parse current full-window telemetry."""

    def fetch_usage(self) -> UsageRecord:
        """Compatibility projection for the GNOME indicator."""
        return usage_from_telemetry(self.fetch_telemetry(), display_name=self.display_name)

    def safe_fetch_telemetry(self) -> TelemetryFetch:
        """Read telemetry without allowing network, auth, or parser errors to escape."""
        try:
            return TelemetryFetch(
                provider_id=self.id,
                display_name=self.display_name,
                telemetry=self.fetch_telemetry(),
            )
        except Exception as exc:  # noqa: BLE001 - provider isolation is the contract
            return TelemetryFetch(
                provider_id=self.id,
                display_name=self.display_name,
                error=str(exc),
            )

    def safe_fetch(self) -> UsageRecord:
        """Legacy GNOME wrapper: never raises, including on strict parser validation."""
        result = self.safe_fetch_telemetry()
        try:
            if result.telemetry is not None:
                return usage_from_telemetry(
                    result.telemetry, display_name=self.display_name
                )
        except Exception as exc:  # noqa: BLE001 - compatibility boundary must not raise
            result = TelemetryFetch(
                provider_id=self.id,
                display_name=self.display_name,
                error=str(exc),
            )
        return UsageRecord(
            provider_id=self.id,
            display_name=self.display_name,
            used=0.0,
            limit=None,
            error=result.error or "unknown provider error",
        )
