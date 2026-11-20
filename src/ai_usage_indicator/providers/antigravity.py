"""Antigravity (Google Antigravity CLI) plan-usage provider.

Reads the OAuth access token that ``agy`` stores in the desktop Secret Service keyring
(``service=gemini``, ``username=antigravity``) and calls the Cloud Code Assist
``fetchAvailableModels`` endpoint for current per-pool model quotas.

Credentials are never written or copied by this adapter. When the access token is expired
(or the endpoint returns 401), we run ``agy models`` headlessly so the CLI performs its
normal silent token refresh into the keyring, then re-read the token.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ai_usage_indicator.net import HttpError, post_json
from ai_usage_indicator.providers.base import Provider, ProviderError
from ai_usage_indicator.telemetry import Telemetry, TelemetryValidationError, parse_iso_datetime
from ai_usage_indicator.telemetry_parsers import snapshot_from_antigravity_models

DEFAULT_USAGE_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
DEFAULT_SUMMARY_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
DEFAULT_KEYRING_SERVICE = "gemini"
DEFAULT_KEYRING_USERNAME = "antigravity"
DEFAULT_COMMAND = "agy"
DEFAULT_CLI_TIMEOUT = 20.0
DEFAULT_SECRET_TOOL_TIMEOUT = 10.0

_FALLBACK_AGY_DIRS = (
    ".local/share/mise/installs/antigravity-cli/latest",
    ".local/share/mise/shims",
    ".local/bin",
)
_SYSTEM_BIN_DIRS = ("/usr/local/bin", "/usr/bin", "/opt/homebrew/bin")
_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def _parse_expiry(value: object) -> datetime | None:
    """Parse Go RFC3339Nano timestamps (e.g. ``2026-11-20T13:05:38.739767591+02:00``)."""
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = _FRAC_RE.sub(r"\1", value.strip())
    try:
        return parse_iso_datetime(normalized)
    except (TelemetryValidationError, ValueError):
        return None


def resolve_agy_command(command: str = DEFAULT_COMMAND) -> str | None:
    """Find the Antigravity CLI without relying on the inherited PATH."""
    if os.path.isabs(command):
        return command if os.access(command, os.X_OK) else None
    found = shutil.which(command)
    if found:
        return found
    home = Path.home()
    candidates = [home / rel for rel in _FALLBACK_AGY_DIRS] + [
        Path(name) for name in _SYSTEM_BIN_DIRS
    ]
    for directory in candidates:
        candidate = directory / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _resolve_secret_tool() -> str | None:
    found = shutil.which("secret-tool")
    if found:
        return found
    for directory in _SYSTEM_BIN_DIRS:
        candidate = Path(directory) / "secret-tool"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _read_keyring_secret(
    service: str = DEFAULT_KEYRING_SERVICE,
    username: str = DEFAULT_KEYRING_USERNAME,
    timeout: float = DEFAULT_SECRET_TOOL_TIMEOUT,
) -> str:
    """Read the raw credential payload stored by ``agy`` in Secret Service."""
    binary = _resolve_secret_tool()
    if binary is None:
        raise ProviderError("secret-tool not found — install libsecret-tools")
    try:
        result = subprocess.run(
            [binary, "lookup", "service", service, "username", username],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError("keyring lookup timed out") from exc
    except OSError as exc:
        raise ProviderError(f"could not run secret-tool: {exc}") from exc

    if result.returncode != 0 or not result.stdout.strip():
        raise ProviderError("not signed in — run `agy` to authenticate")
    return result.stdout


def _invoke_agy_refresh(command: str, timeout: float = DEFAULT_CLI_TIMEOUT) -> None:
    """Run ``agy models`` headlessly so ``agy`` refreshes its keyring OAuth token."""
    env = os.environ.copy()
    # Prevent a failed refresh from launching an interactive browser from systemd.
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    subprocess.run(
        [command, "models"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        env=env,
        check=False,
    )


def _extract_token(raw_payload: str) -> tuple[str, datetime | None]:
    """Parse ``(access_token, expiry)`` from the keyring JSON blob."""
    try:
        blob = json.loads(raw_payload)
    except ValueError as exc:
        raise ProviderError("malformed Antigravity keyring entry") from exc
    if not isinstance(blob, dict):
        raise ProviderError("malformed Antigravity keyring entry")

    token_obj = blob.get("token") if isinstance(blob.get("token"), dict) else blob
    access = token_obj.get("access_token") or token_obj.get("accessToken")
    if not isinstance(access, str) or not access.strip():
        raise ProviderError("no access token found — run `agy` to authenticate")
    expiry = _parse_expiry(token_obj.get("expiry") or token_obj.get("expires_at"))
    return access.strip(), expiry


class AntigravityProvider(Provider):
    id = "antigravity"
    display_name = "Antigravity"

    def __init__(self, provider_id: str = "antigravity", config: dict | None = None) -> None:
        super().__init__(config)
        self.id = provider_id
        self.display_name = self.config.get("display_name", "Antigravity")
        self._usage_url = str(self.config.get("usage_url", DEFAULT_USAGE_URL))
        self._summary_url = str(self.config.get("summary_url", DEFAULT_SUMMARY_URL))
        self._keyring_service = str(
            self.config.get("keyring_service", DEFAULT_KEYRING_SERVICE)
        )
        self._keyring_username = str(
            self.config.get("keyring_username", DEFAULT_KEYRING_USERNAME)
        )
        self._command = str(self.config.get("command", DEFAULT_COMMAND))
        self._cli_timeout = float(self.config.get("timeout_seconds", DEFAULT_CLI_TIMEOUT))
        self._project = str(self.config.get("project") or "").strip()
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._refreshed_this_cycle: bool = False

    def _read_auth(self) -> None:
        raw = _read_keyring_secret(
            service=self._keyring_service,
            username=self._keyring_username,
        )
        self._token, self._expires_at = _extract_token(raw)

    def _token_is_expired(self) -> bool:
        if self._expires_at is None:
            return False
        return self._expires_at <= datetime.now(timezone.utc)

    def _try_cli_refresh(self) -> bool:
        rejected = self._token
        was_expired = self._token_is_expired()
        command = resolve_agy_command(self._command)
        if not command:
            return False
        try:
            _invoke_agy_refresh(command, timeout=self._cli_timeout)
        except (OSError, subprocess.TimeoutExpired):
            return False
        self._read_auth()
        if not self._token or self._token_is_expired():
            return False
        if self._token == rejected and not was_expired:
            return False
        self._refreshed_this_cycle = True
        return True

    def authenticate(self) -> None:
        self._read_auth()
        self._refreshed_this_cycle = False
        if self._token_is_expired():
            if not self._try_cli_refresh():
                raise ProviderError("token expired — run `agy` to refresh")

    def _post_with_auth(self, url: str) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": "antigravity/1.0",
        }
        body = {"project": self._project} if self._project else {}
        try:
            return post_json(url, headers, body)
        except HttpError as exc:
            if exc.status == 401 and not self._refreshed_this_cycle:
                if self._try_cli_refresh():
                    return self._post_with_auth(url)
            if exc.status in (401, 403):
                raise ProviderError("unauthorized — run `agy` to re-auth") from exc
            raise ProviderError(f"HTTP {exc.status}") from exc

    def _fetch_models(self) -> dict:
        return self._post_with_auth(self._usage_url)

    def _fetch_quota_summary(self) -> dict | None:
        """Best-effort fetch of weekly quota buckets from ``retrieveUserQuotaSummary``."""
        if not self._summary_url:
            return None
        try:
            return self._post_with_auth(self._summary_url)
        except (ProviderError, OSError, ValueError):
            return None

    def fetch_telemetry(self) -> Telemetry:
        # Re-read each cycle so a background `agy` token refresh is picked up.
        self.authenticate()
        data = self._fetch_models()
        summary = self._fetch_quota_summary()
        return snapshot_from_antigravity_models(
            data,
            summary=summary,
            observed_at=datetime.now(timezone.utc),
            provider=self.id,
        )
