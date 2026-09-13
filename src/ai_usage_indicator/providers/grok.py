"""Grok (SuperGrok / Grok Build) plan-usage provider.

Reads the OAuth token the Grok CLI stores in ~/.grok/auth.json and calls the same
CLI-proxy billing endpoint the `/usage` slash command uses.

When the access token is expired (or the billing endpoint returns 401), we let
the Grok CLI refresh it the same way a normal ``grok`` start does — no re-login.
If the CLI isn't on PATH, we fall back to an OIDC refresh against the stored
``refresh_token`` and write the new tokens back to ``auth.json``.

Team accounts always get a billing period with no plan percent — the CLI endpoint
simply does not carry team spend. Their real numbers live behind the xAI Management
API (what console.x.ai renders), which needs a separate *management key*; configure
`management_key` and we read actual spend and the team's credit total from there.

Without a management key we fall back to summing local
`~/.grok/sessions/**/usage.json` ledgers for the current period. That is a floor,
not the truth: it misses Grok web-app usage, other machines, and sessions the CLI
never wrote a ledger for.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_usage_indicator.net import HttpError, get_json, post_form, post_json
from ai_usage_indicator.providers.base import Provider, ProviderError
from ai_usage_indicator.telemetry import (
    Confidence,
    QuotaWindow,
    Source,
    Telemetry,
    TelemetryValidationError,
    parse_iso_datetime,
)
from ai_usage_indicator.telemetry_parsers import (
    snapshot_from_grok_billing,
    snapshot_from_xai_console,
    xai_console_credits_usd,
    xai_console_spend_usd,
)
from ai_usage_indicator.usage import UsageRecord, usage_from_telemetry

USAGE_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
MANAGEMENT_BASE = "https://management-api.x.ai"
MANAGEMENT_KEY_ENV = "XAI_MANAGEMENT_KEY"
COST_TICKS_PER_USD = 10**10
# SpaceXAI access tokens last ~6 hours; fall back to that if the IdP omits expires_in.
DEFAULT_ACCESS_TOKEN_LIFETIME = timedelta(hours=6)
DEFAULT_CLI_TIMEOUT = 20.0
_EXPIRED_HINT = "token expired — run `grok` to refresh"
_KNOWN_TOKEN_ENDPOINTS = {
    "https://auth.x.ai": "https://auth.x.ai/oauth2/token",
}


def _default_auth_path() -> Path:
    home = Path(os.environ.get("GROK_HOME", Path.home() / ".grok"))
    return home / "auth.json"


def _pick_named_entry(blob: dict) -> tuple[str | None, dict | None]:
    """Prefer the current SpaceXAI OIDC session, then the legacy accounts.x.ai key."""
    legacy: tuple[str, dict] | None = None
    first: tuple[str, dict] | None = None
    for key, value in blob.items():
        if not isinstance(value, dict) or not value.get("key"):
            continue
        if first is None:
            first = (key, value)
        if str(key).startswith("https://auth.x.ai::"):
            return key, value
        if str(key).startswith("https://accounts.x.ai"):
            legacy = (key, value)
    return legacy or first or (None, None)


def _issuer_and_client(entry_key: str | None, entry: dict) -> tuple[str, str]:
    issuer = str(entry.get("oidc_issuer") or "").rstrip("/")
    client_id = str(entry.get("oidc_client_id") or "")
    if entry_key and "::" in entry_key:
        prefix, suffix = entry_key.split("::", 1)
        if not issuer:
            issuer = prefix.rstrip("/")
        if not client_id:
            client_id = suffix
    return issuer, client_id


def _token_endpoint_for_issuer(issuer: str) -> str:
    issuer = issuer.rstrip("/")
    known = _KNOWN_TOKEN_ENDPOINTS.get(issuer)
    if known:
        return known
    try:
        doc = get_json(
            f"{issuer}/.well-known/openid-configuration",
            {"Accept": "application/json", "User-Agent": "ai-usage-indicator/0.1"},
        )
        endpoint = doc.get("token_endpoint") if isinstance(doc, dict) else None
        if isinstance(endpoint, str) and endpoint.startswith("https://"):
            return endpoint
    except (HttpError, OSError, ValueError, TypeError):
        pass
    return f"{issuer}/oauth2/token"


def _format_expires_at(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _invoke_grok_refresh(command: str, auth_path: Path, timeout: float) -> None:
    """Start Grok headlessly so it performs its normal silent token refresh.

    ``grok models`` loads credentials, refreshes if needed, prints the model list,
    and exits — the same refresh path as opening the TUI, without a login prompt.
    """
    env = os.environ.copy()
    env["GROK_HOME"] = str(auth_path.parent)
    # systemd has a display; don't let a failed refresh pop a browser.
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


def _atomic_write_json(path: Path, blob: dict) -> None:
    payload = json.dumps(blob, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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


def _format_console_time(value: datetime) -> str:
    """Management API time range format: naive UTC ``YYYY-MM-DD HH:MM:SS`` + ``Etc/GMT``."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _console_error(exc: HttpError) -> str:
    if exc.status == 401:
        return "management key rejected — recreate it at console.x.ai"
    if exc.status == 403:
        return "management key lacks billing permission"
    return f"console HTTP {exc.status}"


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
        self._refresh_token: str = ""
        self._oidc_issuer: str = ""
        self._oidc_client_id: str = ""
        self._entry_key: str = ""
        self._refreshed_this_cycle: bool = False
        self._command = str(self.config.get("command", "grok"))
        self._cli_timeout = float(self.config.get("timeout_seconds", DEFAULT_CLI_TIMEOUT))
        self._team_id: str = ""
        # The one credential this project holds itself: an xAI management key, created at
        # console.x.ai → Settings → Management Keys. The CLI's OAuth token cannot reach the
        # billing endpoints (its scopes stop at grok-cli/api/conversations/workspaces).
        self._management_key = str(
            self.config.get("management_key") or os.environ.get(MANAGEMENT_KEY_ENV, "")
        ).strip()
        self._team_id_override = str(self.config.get("team_id") or "").strip()

    def authenticate(self) -> None:
        self._read_auth()
        self._refreshed_this_cycle = False
        if self._token_is_expired():
            self._refresh_access_token()

    def _read_auth(self) -> None:
        if not self._auth_path.exists():
            raise ProviderError("not signed in — run `grok login`")
        blob = json.loads(self._auth_path.read_text())
        entry_key, entry = _pick_named_entry(blob)
        if not entry or not entry.get("key"):
            raise ProviderError("no token found — run `grok login`")
        self._entry_key = entry_key or ""
        self._token = entry["key"]
        self._user_id = str(entry.get("user_id") or "")
        self._principal_type = str(entry.get("principal_type") or "")
        self._team_id = str(entry.get("team_id") or entry.get("principal_id") or "")
        self._refresh_token = str(entry.get("refresh_token") or "")
        self._oidc_issuer, self._oidc_client_id = _issuer_and_client(entry_key, entry)
        expires_raw = entry.get("expires_at")
        self._expires_at = None if not expires_raw else parse_iso_datetime(expires_raw)

    def _token_is_expired(self) -> bool:
        if self._expires_at is None:
            return False
        return self._expires_at <= datetime.now(timezone.utc)

    def _refresh_access_token(self) -> None:
        """Refresh the way starting ``grok`` does; OIDC is only a fallback."""
        if self._try_cli_refresh():
            return
        self._refresh_oidc_token()

    def _resolve_grok_command(self) -> str | None:
        configured = self._command
        if os.path.isabs(configured) and os.access(configured, os.X_OK):
            return configured
        found = shutil.which(configured)
        if found:
            return found
        for candidate in (
            Path.home() / ".local" / "bin" / configured,
            self._auth_path.parent / "bin" / configured,
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    def _try_cli_refresh(self) -> bool:
        rejected = self._token
        was_expired = self._token_is_expired()
        command = self._resolve_grok_command()
        if not command:
            return False
        try:
            _invoke_grok_refresh(command, self._auth_path, self._cli_timeout)
        except (OSError, subprocess.TimeoutExpired):
            return False
        self._read_auth()
        if not self._token or self._token_is_expired():
            return False
        # Unchanged unexpired credentials mean Grok didn't rotate anything (e.g. a 401
        # with a still-valid local expiry). Fall through to OIDC in that case.
        if self._token == rejected and not was_expired:
            return False
        self._refreshed_this_cycle = True
        return True

    def _refresh_oidc_token(self) -> None:
        if (
            not self._refresh_token
            or not self._oidc_issuer.startswith("https://")
            or not self._oidc_client_id
        ):
            raise ProviderError(_EXPIRED_HINT)
        rejected = self._token
        endpoint = _token_endpoint_for_issuer(self._oidc_issuer)
        try:
            payload = post_form(
                endpoint,
                {
                    "Accept": "application/json",
                    "User-Agent": "ai-usage-indicator/0.1",
                },
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._oidc_client_id,
                },
            )
        except HttpError as exc:
            # Grok may have rotated the refresh token while we were in flight.
            self._read_auth()
            if self._token and self._token != rejected and not self._token_is_expired():
                self._refreshed_this_cycle = True
                return
            raise ProviderError(_EXPIRED_HINT) from exc
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise ProviderError(_EXPIRED_HINT)
        new_refresh = payload.get("refresh_token")
        refresh = (
            new_refresh
            if isinstance(new_refresh, str) and new_refresh
            else self._refresh_token
        )
        expires_in = payload.get("expires_in")
        now = datetime.now(timezone.utc)
        if (
            isinstance(expires_in, (int, float))
            and not isinstance(expires_in, bool)
            and expires_in > 0
        ):
            expires_at = now + timedelta(seconds=float(expires_in))
        else:
            expires_at = now + DEFAULT_ACCESS_TOKEN_LIFETIME
        self._token = access
        self._refresh_token = refresh
        self._expires_at = expires_at
        self._refreshed_this_cycle = True
        self._persist_refreshed_tokens(access, refresh, expires_at, previous=rejected)

    def _persist_refreshed_tokens(
        self,
        access: str,
        refresh: str,
        expires_at: datetime,
        *,
        previous: str | None,
    ) -> None:
        """Write the new tokens back to the CLI's auth.json, or adopt a sibling refresh."""
        try:
            blob = json.loads(self._auth_path.read_text())
        except (OSError, ValueError):
            return
        entry_key, entry = _pick_named_entry(blob)
        if self._entry_key and isinstance(blob.get(self._entry_key), dict):
            entry_key = self._entry_key
            entry = blob[self._entry_key]
        if not isinstance(entry, dict):
            return
        disk_key = entry.get("key")
        disk_expires = None
        try:
            if entry.get("expires_at"):
                disk_expires = parse_iso_datetime(entry.get("expires_at"))
        except (TelemetryValidationError, ValueError):
            disk_expires = None
        # Another Grok process already stored a different unexpired token — use it.
        # The token we just refreshed away is still on disk until we write; ignore it.
        if (
            isinstance(disk_key, str)
            and disk_key
            and disk_key != access
            and disk_key != previous
            and disk_expires is not None
            and disk_expires > datetime.now(timezone.utc)
        ):
            self._token = disk_key
            self._refresh_token = str(entry.get("refresh_token") or refresh)
            self._expires_at = disk_expires
            self._refreshed_this_cycle = True
            return
        entry["key"] = access
        entry["refresh_token"] = refresh
        entry["expires_at"] = _format_expires_at(expires_at)
        if entry_key is None:
            return
        blob[entry_key] = entry
        try:
            _atomic_write_json(self._auth_path, blob)
        except OSError:
            # This cycle can still use the in-memory token.
            return

    # ----------------------------------------------------------------- xAI Management API

    @property
    def _console_team_id(self) -> str:
        return self._team_id_override or self._team_id

    def _console_ready(self) -> bool:
        return bool(self._management_key and self._console_team_id)

    def _console_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._management_key}",
            "Accept": "application/json",
            "User-Agent": "ai-usage-indicator/0.1",
        }

    def _console_spend_usd(self, start: datetime, end: datetime) -> float:
        """Actual USD spent in ``[start, end)`` — the Usage Explorer's own numbers.

        ``end`` is exclusive: asking for the period end yields no point on that day.
        """
        body = {
            "analyticsRequest": {
                "timeRange": {
                    "startTime": _format_console_time(start),
                    "endTime": _format_console_time(end),
                    "timezone": "Etc/GMT",
                },
                "timeUnit": "TIME_UNIT_DAY",
                "values": [{"name": "usd", "aggregation": "AGGREGATION_SUM"}],
                "groupBy": [],
                "filters": [],
            }
        }
        url = f"{MANAGEMENT_BASE}/v1/billing/teams/{self._console_team_id}/usage"
        try:
            return xai_console_spend_usd(post_json(url, self._console_headers(), body))
        except HttpError as exc:
            raise ProviderError(_console_error(exc)) from exc

    def _console_credits_usd(self) -> float | None:
        """The team's credit total, so the denominator need not be configured by hand."""
        url = (
            f"{MANAGEMENT_BASE}/v1/billing/teams/"
            f"{self._console_team_id}/postpaid/invoice/preview"
        )
        try:
            return xai_console_credits_usd(get_json(url, self._console_headers()))
        except HttpError as exc:
            raise ProviderError(_console_error(exc)) from exc

    def _console_figures(self, data: dict) -> tuple[float, float | None, datetime]:
        """(spent USD, credit total USD or None, period end) for the current window."""
        start, end = self._period_window(data)
        used = self._console_spend_usd(start, end)
        credits = self._console_credits_usd()
        if credits is None or credits <= 0:
            credits = self._allowance_usd()
        return used, credits, end

    def _console_telemetry(self, data: dict) -> Telemetry:
        start, end = self._period_window(data)
        used = self._console_spend_usd(start, end)
        credits = self._console_credits_usd() or self._allowance_usd()
        if credits is None:
            raise ProviderError(
                "no credit total from console — set allowance_usd to supply one"
            )
        return snapshot_from_xai_console(
            used_usd=used,
            credits_usd=credits,
            period_start=start,
            period_end=end,
            observed_at=datetime.now(timezone.utc),
            provider=self.id,
        )

    def _console_record(self, data: dict) -> UsageRecord:
        used, credits, end = self._console_figures(data)
        return UsageRecord(
            provider_id=self.id,
            display_name=self.display_name,
            used=used,
            limit=credits,
            unit="USD",
            label=(
                f"{format_usd(used)} / {format_usd(credits)}"
                if credits
                else format_usd(used)
            ),
            reset_at=end,
        )

    # ------------------------------------------------------------------------- fetching

    def fetch_telemetry(self) -> Telemetry:
        # Re-read each cycle so a background `grok` token refresh is picked up.
        self.authenticate()

        data = self._get_billing()
        if billing_has_usage_percent(data):
            return snapshot_from_grok_billing(
                data,
                observed_at=datetime.now(timezone.utc),
                provider=self.id,
            )
        # A configured management key is authoritative: never quietly fall back to the local
        # ledger, which under-reports. A console failure surfaces as a provider error.
        if self._console_ready():
            return self._console_telemetry(data)
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
        """GNOME path: plan-usage API, else console spend, else local session spend."""
        try:
            self.authenticate()
            data = self._get_billing()
            if billing_has_usage_percent(data):
                return usage_from_telemetry(
                    snapshot_from_grok_billing(
                        data,
                        observed_at=datetime.now(timezone.utc),
                        provider=self.id,
                    ),
                    display_name=self.display_name,
                )
            if self._console_ready():
                return self._console_record(data)
            if self._principal_type.lower() != "team":
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
            if exc.status == 401 and not self._refreshed_this_cycle:
                self._refresh_access_token()
                return self._get_billing()  # _refreshed_this_cycle prevents a loop
            if exc.status in (401, 403):
                raise ProviderError("unauthorized — run `grok` to refresh") from exc
            raise ProviderError(f"HTTP {exc.status}") from exc
