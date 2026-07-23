"""Shared read-only telemetry collection API."""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_usage_indicator.config import Config, load_config
from ai_usage_indicator.providers import Provider, build_provider
from ai_usage_indicator.telemetry import SCHEMA_VERSION, Telemetry


@dataclass(frozen=True)
class ProviderFailure:
    """One isolated provider failure in a collection."""

    provider: str
    display_name: str
    error: str

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "error": self.error,
        }


@dataclass
class TelemetryCollection:
    """Typed result shared by the Python API and JSON CLI."""

    telemetry: list[Telemetry] = field(default_factory=list)
    errors: list[ProviderFailure] = field(default_factory=list)

    def to_envelope(self) -> dict:
        """Serialize with t2's schema version and canonical telemetry dictionaries."""
        return {
            "schema_version": SCHEMA_VERSION,
            "telemetry": [snapshot.to_dict() for snapshot in self.telemetry],
            "errors": [failure.to_dict() for failure in self.errors],
        }


def providers_from_config(config: Config) -> list[Provider]:
    """Build configured providers without authenticating or performing I/O."""
    return [build_provider(entry) for entry in config.providers]


def collect_telemetry(
    providers: list[Provider] | None = None,
    *,
    config: Config | None = None,
) -> TelemetryCollection:
    """Collect all providers, containing each failure while preserving valid peers.

    Passing providers directly is useful for embedders and tests. When omitted, config is
    read without creating files, keeping this API observation-only.
    """
    if providers is None:
        providers = providers_from_config(config or load_config(create=False))

    collection = TelemetryCollection()
    for provider in providers:
        result = provider.safe_fetch_telemetry()
        if result.telemetry is not None:
            collection.telemetry.append(result.telemetry)
        else:
            collection.errors.append(
                ProviderFailure(
                    provider=result.provider_id,
                    display_name=result.display_name,
                    error=result.error or "unknown provider error",
                )
            )
    return collection
