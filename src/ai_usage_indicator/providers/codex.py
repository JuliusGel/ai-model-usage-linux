"""Codex (ChatGPT plan) usage provider.

Reads the OAuth token the Codex CLI stores locally and calls the same ChatGPT backend
usage endpoint the CLI uses. Undocumented endpoint → defensive parsing, errors surfaced
as an error record rather than raised.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ai_usage_indicator.net import HttpError, get_json, post_json
from ai_usage_indicator.providers.base import Provider, atomic_write_json
from ai_usage_indicator.usage import UsageRecord

DEFAULT_AUTH = Path.home() / ".codex" / "auth.json"
USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
# Undocumented, best-effort — used only when auto_refresh is explicitly enabled.
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _window_name(seconds: int | None) -> str:
    """Short human label for a rate-limit window given its length in seconds."""
    if not seconds:
        return "win"
    hours = seconds / 3600
    if hours <= 6:
        return f"{round(hours)}h"
    if hours <= 24:
        return f"{round(hours)}h"
    days = seconds / 86400
    if abs(days - 7) < 0.5:
        return "wk"
    return f"{round(days)}d"


class CodexProvider(Provider):
    id = "codex"
    display_name = "Codex"

    def __init__(self, provider_id: str = "codex", config: dict | None = None) -> None:
        super().__init__(config)
        self.id = provider_id
        self.display_name = self.config.get("display_name", "Codex")
        self._auth_path = Path(self.config.get("auth_path", DEFAULT_AUTH))
        self._token: str | None = None
        self._account_id: str | None = None

    def authenticate(self) -> None:
        tokens = json.loads(self._auth_path.read_text())["tokens"]
        self._token = tokens["access_token"]
        self._account_id = tokens.get("account_id")

    def _err(self, message: str) -> UsageRecord:
        return UsageRecord(self.id, self.display_name, 0.0, None, error=message)

    def _try_refresh(self) -> bool:
        """Opt-in OAuth refresh; writes new tokens back to the shared auth.json so the
        `codex` CLI keeps working. Only writes on success."""
        try:
            blob = json.loads(self._auth_path.read_text())
            tokens = blob["tokens"]
            resp = post_json(
                TOKEN_URL,
                {"User-Agent": "ai-usage-indicator/0.1"},
                {
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                    "client_id": CLIENT_ID,
                },
            )
        except (HttpError, KeyError, OSError, ValueError):
            return False
        if "access_token" not in resp:
            return False
        tokens["access_token"] = resp["access_token"]
        if resp.get("refresh_token"):
            tokens["refresh_token"] = resp["refresh_token"]
        if resp.get("id_token"):
            tokens["id_token"] = resp["id_token"]
        blob["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        atomic_write_json(self._auth_path, blob)
        self._token = tokens["access_token"]
        return True

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "chatgpt-account-id": self._account_id or "",
            "Accept": "application/json",
            "User-Agent": "ai-usage-indicator/0.1",
        }

    def fetch_usage(self) -> UsageRecord:
        self.authenticate()  # re-read so a background `codex` refresh is picked up
        try:
            data = get_json(USAGE_URL, self._headers())
        except HttpError as exc:
            if exc.status in (401, 403):
                # Token likely expired; refresh once and retry if the user opted in.
                if self.config.get("auto_refresh") and self._try_refresh():
                    try:
                        data = get_json(USAGE_URL, self._headers())
                    except HttpError:
                        return self._err("unauthorized — run `codex` to re-auth")
                else:
                    return self._err("unauthorized — run `codex` to re-auth")
            else:
                return self._err(f"HTTP {exc.status}")

        rate = data.get("rate_limit") or {}
        windows = [w for w in (rate.get("primary_window"), rate.get("secondary_window")) if w]
        if not windows:
            return self._err("no rate-limit data")

        parts = []
        headline_used = -1.0
        headline_reset: datetime | None = None
        for win in windows:
            pct = float(win.get("used_percent") or 0.0)
            name = _window_name(win.get("limit_window_seconds"))
            parts.append(f"{name} {round(pct)}%")
            if pct > headline_used:
                headline_used = pct
                ts = win.get("reset_at")
                headline_reset = (
                    datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
                )

        return UsageRecord(
            provider_id=self.id,
            display_name=self.display_name,
            used=max(headline_used, 0.0),
            limit=100.0,
            label=" · ".join(parts),
            reset_at=headline_reset,
        )
