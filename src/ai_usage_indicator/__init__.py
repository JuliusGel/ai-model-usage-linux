"""AI subscription usage indicator for the Ubuntu system tray."""

from ai_usage_indicator.core import (
    ProviderFailure,
    TelemetryCollection,
    collect_telemetry,
)
from ai_usage_indicator.telemetry import (
    SCHEMA_VERSION,
    Confidence,
    QuotaWindow,
    Source,
    Telemetry,
)

__version__ = "0.1.0"
APP_ID = "ai-usage-indicator"

__all__ = [
    "SCHEMA_VERSION",
    "Confidence",
    "ProviderFailure",
    "QuotaWindow",
    "Source",
    "Telemetry",
    "TelemetryCollection",
    "collect_telemetry",
]
