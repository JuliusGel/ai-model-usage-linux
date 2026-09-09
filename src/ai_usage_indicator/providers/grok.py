"""Grok (SuperGrok / Grok Build) plan-usage provider.

Reads the OAuth token the Grok CLI stores in ~/.grok/auth.json and calls the same
CLI-proxy billing endpoint the `/usage` slash command uses. Credentials are never
modified; the Grok CLI remains responsible for refresh and re-authentication.

Team accounts often get a billing period with no plan percent. In that case we
fall back to summing local `~/.grok/sessions/**/usage.json` ledgers for the
current period (cost + tokens the CLI already persisted).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_usage_indicator.net import HttpError, get_json
from ai_usage_indicator.providers.base import Provider, ProviderError
from ai_usage_indicator.telemetry import (
    Confidence,
    QuotaWindow,
    Source,
    Telemetry,
    TelemetryValidationError,
    parse_iso_datetime,
)
from ai_usage_indicator.telemetry_parsers import snapshot_from_grok_billing
from ai_usage_indicator.usage import UsageRecord, usage_from_telemetry

USAGE_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
COST_TICKS_PER_USD = 10**10


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


@dataclass(frozen=True)
class LocalSpend:
    usd: float
    tokens: int
    sessions: int


def _try_parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_iso_datetime(value)
    except (TelemetryValidationError, ValueError):
        return None


def billing_period_bounds(payload: dict) -> tuple[datetime | None, datetime | None]:
    """Start/end of the current Grok billing window, if the payload carries them."""
    cfg = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(cfg, dict):
        return None, None
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    start = _try_parse_iso(period.get("start") or cfg.get("billingPeriodStart"))
    end = _try_parse_iso(period.get("end") or cfg.get("billingPeriodEnd"))
    return start, end


def billing_has_usage_percent(payload: dict) -> bool:
    cfg = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(cfg, dict):
        return False
    if cfg.get("creditUsagePercent") is not None:
        return True
    monthly = cfg.get("monthlyLimit")
    used = cfg.get("used")
    limit_val = monthly.get("val") if isinstance(monthly, dict) else None
    used_val = used.get("val") if isinstance(used, dict) else None
    return isinstance(limit_val, (int, float)) and limit_val > 0 and used_val is not None


def format_usd(usd: float) -> str:
    if usd <= 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.3f}"
    return f"${usd:.2f}"


def format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M tok"
    if count >= 1000:
        return f"{count / 1000:.0f}k tok"
    return f"{count} tok"


def scan_local_usage(sessions_root: Path, start: datetime, end: datetime) -> LocalSpend:
    """Sum CLI usage ledgers whose turns fall in ``[start, end)``."""
    usd_ticks = 0
    tokens = 0
    sessions = 0
    if not sessions_root.is_dir():
        return LocalSpend(0.0, 0, 0)
    for path in sessions_root.rglob("usage.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        session = data.get("session") if isinstance(data.get("session"), dict) else {}
        turns = data.get("turns") if isinstance(data.get("turns"), list) else []
        counted = False
        if turns:
            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                ended = _try_parse_iso(turn.get("endedAt"))
                if ended is None or ended < start or ended >= end:
                    continue
                usd_ticks += int(turn.get("costUsdTicks") or 0)
                tokens += int(turn.get("totalTokens") or 0)
                counted = True
        else:
            updated = _try_parse_iso(data.get("updatedAt"))
            if updated is not None and start <= updated < end:
                usd_ticks += int(session.get("costUsdTicks") or 0)
                tokens += int(session.get("totalTokens") or 0)
                counted = True
        if counted:
            sessions += 1
    return LocalSpend(usd_ticks / COST_TICKS_PER_USD, tokens, sessions)


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
        self._principal_type: str = ""
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
        self._principal_type = str(entry.get("principal_type") or "")
        expires_raw = entry.get("expires_at")
        self._expires_at = None if not expires_raw else parse_iso_datetime(expires_raw)

    def fetch_telemetry(self) -> Telemetry:
        # Re-read each cycle so a background `grok` token refresh is picked up.
        self.authenticate()
        if self._expires_at and self._expires_at <= datetime.now(timezone.utc):
            raise ProviderError("token expired — run `grok login` to refresh")

        data = self._get_billing()
        if billing_has_usage_percent(data):
            return snapshot_from_grok_billing(
                data,
                observed_at=datetime.now(timezone.utc),
                provider=self.id,
            )
        derived = self._derived_team_telemetry(data)
        if derived is not None:
            return derived
        if self._principal_type.lower() == "team":
            raise ProviderError("team plan usage not reported by Grok")
        return snapshot_from_grok_billing(
            data,
            observed_at=datetime.now(timezone.utc),
            provider=self.id,
        )

    def safe_fetch(self) -> UsageRecord:
        """GNOME path: prefer the plan-usage API, else local session spend."""
        try:
            self.authenticate()
            if self._expires_at and self._expires_at <= datetime.now(timezone.utc):
                raise ProviderError("token expired — run `grok login` to refresh")
            data = self._get_billing()
            if billing_has_usage_percent(data) or self._principal_type.lower() != "team":
                return usage_from_telemetry(
                    snapshot_from_grok_billing(
                        data,
                        observed_at=datetime.now(timezone.utc),
                        provider=self.id,
                    ),
                    display_name=self.display_name,
                )
            return self._local_spend_record(data)
        except Exception as exc:  # noqa: BLE001 - GNOME wrapper must not raise
            return UsageRecord(self.id, self.display_name, 0.0, None, error=str(exc))

    def _allowance_usd(self) -> float | None:
        raw = self.config.get("allowance_usd")
        if raw is None or isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _period_window(self, data: dict) -> tuple[datetime, datetime]:
        start, end = billing_period_bounds(data)
        now = datetime.now(timezone.utc)
        if end is None:
            end = now
        if start is None:
            start = end - timedelta(days=7)
        return start, end

    def _derived_team_telemetry(self, data: dict) -> Telemetry | None:
        """Build a derived weekly window from local spend / configured USD allowance."""
        if self._principal_type.lower() != "team":
            return None
        allowance = self._allowance_usd()
        if allowance is None:
            return None
        start, end = self._period_window(data)
        spend = scan_local_usage(self._auth_path.parent / "sessions", start, end)
        used_fraction = min(1.0, max(0.0, spend.usd / allowance))
        duration = (end - start).total_seconds()
        return Telemetry(
            provider=self.id,
            observed_at=datetime.now(timezone.utc),
            source=Source.UNKNOWN,
            confidence=Confidence.DERIVED,
            windows=[
                QuotaWindow.from_used_fraction(
                    id="weekly",
                    name="Weekly",
                    used_fraction=used_fraction,
                    duration_seconds=duration if duration > 0 else None,
                    resets_at=end,
                )
            ],
        )

    def _local_spend_record(self, data: dict) -> UsageRecord:
        start, end = self._period_window(data)
        spend = scan_local_usage(self._auth_path.parent / "sessions", start, end)
        allowance = self._allowance_usd()
        if allowance is not None:
            return UsageRecord(
                provider_id=self.id,
                display_name=self.display_name,
                used=spend.usd,
                limit=allowance,
                unit="USD",
                label=f"{format_usd(spend.usd)} / {format_usd(allowance)} · {format_tokens(spend.tokens)}",
                reset_at=end,
            )
        return UsageRecord(
            provider_id=self.id,
            display_name=self.display_name,
            used=spend.usd,
            limit=None,
            unit="USD",
            label=f"{format_usd(spend.usd)} · {format_tokens(spend.tokens)}",
            reset_at=end,
        )

    def _get_billing(self) -> dict:
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
            return get_json(USAGE_URL, headers)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise ProviderError("unauthorized — run `grok login` to re-auth") from exc
            raise ProviderError(f"HTTP {exc.status}") from exc
