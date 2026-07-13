"""Claude plan-usage provider.

Reads the OAuth access token that Claude Code already stores locally and calls the same
account usage endpoint the official client uses. This is an undocumented endpoint, so all
parsing stays defensive and failures surface as an error record (never a raised exception).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from ai_usage_indicator.net import HttpError, get_json, post_json
from ai_usage_indicator.providers.base import Provider, atomic_write_json
from ai_usage_indicator.usage import UsageRecord

DEFAULT_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Undocumented, best-effort — used only when auto_refresh is explicitly enabled.
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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

    def _err(self, message: str) -> UsageRecord:
        return UsageRecord(self.id, self.display_name, 0.0, None, error=message)

    def _try_refresh(self) -> bool:
        """Opt-in OAuth refresh. Writes new tokens back to the shared credentials file so
        the `claude` CLI keeps working too. Returns True on success. Only ever writes on a
        successful refresh, so a failure leaves the file untouched."""
        try:
            blob = json.loads(self._cred_path.read_text())
            oauth = blob["claudeAiOauth"]
            resp = post_json(
                TOKEN_URL,
                {"User-Agent": "ai-usage-indicator/0.1"},
                {
                    "grant_type": "refresh_token",
                    "refresh_token": oauth["refreshToken"],
                    "client_id": CLIENT_ID,
                },
            )
        except (HttpError, KeyError, OSError, ValueError):
            return False
        if "access_token" not in resp:
            return False
        oauth["accessToken"] = resp["access_token"]
        if resp.get("refresh_token"):
            oauth["refreshToken"] = resp["refresh_token"]
        if resp.get("expires_in"):
            oauth["expiresAt"] = int((time.time() + float(resp["expires_in"])) * 1000)
        atomic_write_json(self._cred_path, blob)
        self._token = oauth["accessToken"]
        self._expires_at = oauth.get("expiresAt", 0) / 1000.0
        return True

    def fetch_usage(self) -> UsageRecord:
        # Re-read the file each cycle so a background `claude` token refresh is picked up.
        self.authenticate()
        if self._expires_at and self._expires_at < time.time():
            if self.config.get("auto_refresh") and self._try_refresh():
                pass  # token refreshed in place
            else:
                return self._err("token expired — run `claude` to refresh")

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
                return self._err("unauthorized — run `claude` to re-auth")
            return self._err(f"HTTP {exc.status}")

        five = data.get("five_hour") or {}
        seven = data.get("seven_day") or {}
        util_5h = float(five.get("utilization") or 0.0)
        util_7d = float(seven.get("utilization") or 0.0)

        # Headline = the more-constrained window; it drives the tray color and reset time.
        if util_7d > util_5h:
            used, reset_at = util_7d, _parse_iso(seven.get("resets_at"))
        else:
            used, reset_at = util_5h, _parse_iso(five.get("resets_at"))

        label = f"5h {round(util_5h)}% · wk {round(util_7d)}%"
        return UsageRecord(
            provider_id=self.id,
            display_name=self.display_name,
            used=used,
            limit=100.0,
            label=label,
            reset_at=reset_at,
        )
