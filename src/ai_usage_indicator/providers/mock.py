"""Mock provider — slice 1. No credentials; produces deterministic-ish usage so the tray,
glance label, pressure colors, and click menu can be verified before wiring real APIs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai_usage_indicator.providers.base import Provider
from ai_usage_indicator.usage import UsageRecord


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

    def fetch_usage(self) -> UsageRecord:
        reset_at = None
        hours = self.config.get("reset_in_hours")
        if hours is not None:
            reset_at = datetime.now(timezone.utc) + timedelta(hours=float(hours))
        return UsageRecord(
            provider_id=self.id,
            display_name=self.display_name,
            used=float(self.config.get("used", 42)),
            limit=(None if self.config.get("limit") is None else float(self.config["limit"])),
            unit=self.config.get("unit", ""),
            label=self.config.get("label"),
            reset_at=reset_at,
        )
