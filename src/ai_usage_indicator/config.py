"""Config loading. Lives in ~/.config/ai-usage-indicator/, never in the repo.

The config file is TOML. On first run a default (mock-provider) config is written with
0600 perms so secrets added later stay private.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ai-usage-indicator"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULT_REFRESH_SECONDS = 300

# Default: the real providers. They read tokens the official CLIs already store locally —
# no credentials live here. `type = "mock"` is still available for testing.
DEFAULT_CONFIG_TOML = """\
# ai-usage-indicator configuration
# Providers are additive — add a [[providers]] block with a supported `type`
# (claude, codex, grok, mock). Authentication remains owned by the official CLIs.
refresh_seconds = 300

[[providers]]
id = "claude"
type = "claude"
display_name = "Claude"

[[providers]]
id = "codex"
type = "codex"
display_name = "Codex"

[[providers]]
id = "grok"
type = "grok"
display_name = "Grok"
# Team accounts get no plan percent from Grok's CLI endpoint. Point the adapter at
# the xAI Management API (what console.x.ai shows) for real spend and the team's
# credit total. Create the key at console.x.ai -> Settings -> Management Keys; the
# Grok CLI's own OAuth token cannot reach billing. Also read from $XAI_MANAGEMENT_KEY.
# management_key = "xai-..."
# team_id = "..."          # optional; defaults to the team in ~/.grok/auth.json
# Fallback when no management key is set: local CLI spend against this USD allowance.
# Under-reports — it cannot see web-app usage or other machines.
# allowance_usd = 150
"""


@dataclass
class Config:
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS
    providers: list[dict] = field(default_factory=list)
    # Retained only so an old config still parses. Grok OIDC refresh is always on
    # when auth.json still has a refresh_token; this flag is not consulted.
    auto_refresh: bool = False


def ensure_default_config() -> None:
    """Create the config dir + default file if missing, with restrictive perms."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML)
        CONFIG_PATH.chmod(0o600)


def load_config(*, create: bool = True) -> Config:
    """Load configuration.

    ``create=False`` is used by the reusable telemetry API and JSON command, ensuring a
    read-only observation cannot create configuration as a side effect.
    """
    if create:
        ensure_default_config()
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as fh:
            data = tomllib.load(fh)
    else:
        data = tomllib.loads(DEFAULT_CONFIG_TOML)
    return Config(
        refresh_seconds=int(data.get("refresh_seconds", DEFAULT_REFRESH_SECONDS)),
        providers=list(data.get("providers", [])),
        auto_refresh=bool(data.get("auto_refresh", False)),
    )
