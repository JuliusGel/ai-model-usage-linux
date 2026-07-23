"""Mock provider — slice 1. No credentials; produces deterministic-ish usage so the tray,
glance label, pressure colors, and click menu can be verified before wiring real APIs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai_usage_indicator.providers.base import Provider
from ai_usage_indicator.telemetry import Confidence, QuotaWindow, Source, Telemetry


class MockProvider(Provider):
    """Returns a fixed usage record configured at construction time.

    Config keys: ``display_name``, ``used``, ``limit``, ``unit``, ``label``,
    ``reset_in_hours``.
    """

    def __init__(self, provider_id: str, config: dict | None = None) -> None:
        super().__init__(config)
        self.id = provider_id
        self.display_name = self.config.get("display_name", provider_id.title())

    def authenticate(self) -> None:  # no credentials for the mock
        return None

    def fetch_telemetry(self) -> Telemetry:
        reset_at = None
        hours = self.config.get("reset_in_hours")
        if hours is not None:
            reset_at = datetime.now(timezone.utc) + timedelta(hours=float(hours))
        used = float(self.config.get("used", 42))
        limit = float(self.config.get("limit", 100))
        return Telemetry(
            provider=self.id,
            observed_at=datetime.now(timezone.utc),
            source=Source.FIXTURE,
            confidence=Confidence.DERIVED,
            windows=[
                QuotaWindow.from_used_fraction(
                    id="configured",
                    name="Configured",
                    used_fraction=used / limit,
                    resets_at=reset_at,
                )
            ],
        )
