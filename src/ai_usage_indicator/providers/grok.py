"""Grok (SuperGrok / Grok Build) plan-usage provider.

Reads the OAuth token the Grok CLI stores in ~/.grok/auth.json and calls the same
CLI-proxy billing endpoint the `/usage` slash command uses. Credentials are never
modified; the Grok CLI remains responsible for refresh and re-authentication.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ai_usage_indicator.net import HttpError, get_json
from ai_usage_indicator.providers.base import Provider, ProviderError
from ai_usage_indicator.telemetry import Telemetry, parse_iso_datetime
from ai_usage_indicator.telemetry_parsers import snapshot_from_grok_billing

USAGE_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"


def _default_auth_path() -> Path:
    home = Path(os.environ.get("GROK_HOME", Path.home() / ".grok"))
    return home / "auth.json"


def _pick_entry(blob: dict) -> dict | None:
    """Prefer the current SpaceXAI OIDC session, then the legacy accounts.x.ai key."""
    preferred = None
    legacy = None
    first = None
    for key, value in blob.items():
        if not isinstance(value, dict) or not value.get("key"):
            continue
        if first is None:
            first = value
        if str(key).startswith("https://auth.x.ai::"):
            return value
        if str(key).startswith("https://accounts.x.ai"):
            legacy = value
    return preferred or legacy or first


def _read_cli_version(auth_path: Path) -> str:
    version_path = auth_path.parent / "version.json"
    try:
        data = json.loads(version_path.read_text())
        return str(data.get("version") or "0.0.0")
    except (OSError, ValueError, TypeError):
        return "0.0.0"


class GrokProvider(Provider):
    id = "grok"
    display_name = "Grok"

    def __init__(self, provider_id: str = "grok", config: dict | None = None) -> None:
        super().__init__(config)
        self.id = provider_id
        self.display_name = self.config.get("display_name", "Grok")
        self._auth_path = Path(self.config.get("auth_path", _default_auth_path()))
        self._token: str | None = None
        self._user_id: str = ""
        self._expires_at: datetime | None = None

    def authenticate(self) -> None:
        if not self._auth_path.exists():
            raise ProviderError("not signed in — run `grok login`")
        blob = json.loads(self._auth_path.read_text())
        entry = _pick_entry(blob)
        if not entry or not entry.get("key"):
            raise ProviderError("no token found — run `grok login`")
        self._token = entry["key"]
        self._user_id = str(entry.get("user_id") or "")
        expires_raw = entry.get("expires_at")
        self._expires_at = None if not expires_raw else parse_iso_datetime(expires_raw)

    def fetch_telemetry(self) -> Telemetry:
        # Re-read each cycle so a background `grok` token refresh is picked up.
        self.authenticate()
        if self._expires_at and self._expires_at <= datetime.now(timezone.utc):
            raise ProviderError("token expired — run `grok login` to refresh")

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-grok-client-version": _read_cli_version(self._auth_path),
            "x-grok-client-mode": "headless",
            "User-Agent": "ai-usage-indicator/0.1",
        }
        if self._user_id:
            headers["x-userid"] = self._user_id

        try:
            data = get_json(USAGE_URL, headers)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise ProviderError("unauthorized — run `grok login` to re-auth") from exc
            raise ProviderError(f"HTTP {exc.status}") from exc

        return snapshot_from_grok_billing(
            data,
            observed_at=datetime.now(timezone.utc),
            provider=self.id,
        )
