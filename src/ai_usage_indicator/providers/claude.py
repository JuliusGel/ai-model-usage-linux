"""Claude plan-usage provider.

Reads the OAuth access token that Claude Code already stores locally and calls the same
account usage endpoint the official client uses. Credentials are never modified; Claude
Code remains responsible for refresh and re-authentication.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ai_usage_indicator.net import HttpError, get_json
from ai_usage_indicator.providers.base import Provider, ProviderError
from ai_usage_indicator.telemetry import Telemetry
from ai_usage_indicator.telemetry_parsers import snapshot_from_claude_usage

DEFAULT_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


class ClaudeProvider(Provider):
    id = "claude"
    display_name = "Claude"

    def __init__(self, provider_id: str = "claude", config: dict | None = None) -> None:
        super().__init__(config)
        self.id = provider_id
        self.display_name = self.config.get("display_name", "Claude")
        self._cred_path = Path(self.config.get("credentials_path", DEFAULT_CREDENTIALS))
        self._token: str | None = None
        self._expires_at: float = 0.0

    def authenticate(self) -> None:
        data = json.loads(self._cred_path.read_text())["claudeAiOauth"]
        self._token = data["accessToken"]
        self._expires_at = data.get("expiresAt", 0) / 1000.0

    def fetch_telemetry(self) -> Telemetry:
        # Re-read the file each cycle so a background `claude` token refresh is picked up.
        self.authenticate()
        if self._expires_at and self._expires_at < time.time():
            raise ProviderError("token expired — run `claude` to refresh")

        headers = {
            "Authorization": f"Bearer {self._token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "User-Agent": "ai-usage-indicator/0.1",
        }
        try:
            data = get_json(USAGE_URL, headers)
        except HttpError as exc:
            if exc.status == 401:
                raise ProviderError("unauthorized — run `claude` to re-auth") from exc
            raise ProviderError(f"HTTP {exc.status}") from exc

        return snapshot_from_claude_usage(
            data,
            observed_at=datetime.now(timezone.utc),
            provider=self.id,
        )
