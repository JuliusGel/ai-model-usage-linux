# AI Usage Indicator

Shows how much of your AI subscription **plan usage** you've consumed across multiple
providers, inline in the **GNOME top bar** — a "battery indicator for your AI plans."

- **Glanceable:** each provider appears as `[initial] [bar] [percent]`, colored by pressure
  (green → amber → red). All providers visible at once, no clicking required.
- **Details on click:** a popup lists each provider's usage windows, reset times, last
  update, and a Refresh action.
- **Multiple providers** behind a plugin interface. Ships with **Claude**, **Codex**, and **Grok**.

## Architecture (hybrid)

Two cooperating pieces:

1. **Telemetry core** (`src/ai_usage_indicator/`, pure-stdlib Python) — one read-only,
   UI-agnostic provider layer with a versioned full-window schema. It exposes both the
   `ai-model-usage --json` command and importable `ai_model_usage` Python API. The desktop
   service projects those snapshots into its existing
   `~/.cache/ai-usage-indicator/state.json` format.
2. **GNOME Shell extension** (`gnome-extension/`) — pure presentation. It reads `state.json`
   and renders the panel widget + popup. It never calls any API itself.

This keeps the tested provider layer in Python (adding a provider is a Python change) while
getting a native-looking panel widget that a tray/AppIndicator icon can't provide.

Why an extension instead of a tray icon: the AppIndicator/StatusNotifierItem protocol is
limited to one icon + one short label. Rich inline widgets (bars, multiple values, styled
popups) require a GNOME Shell extension — the same mechanism Vitals/TopHat use.

## Requirements

- Ubuntu with GNOME Shell 48–50, Python 3.11+.
- No third-party Python packages (backend is stdlib-only).

## Install

```bash
./install.sh
```

This creates a venv, installs the backend, enables the `systemd --user` service, and copies
the extension into `~/.local/share/gnome-shell/extensions/`. Then:

```bash
# Wayland can't load a newly-installed extension without a fresh session:
#   log out and back in, then:
gnome-extensions enable ai-usage-indicator@matom.ai
```

## Providers & data sources

Each provider reuses the token its official CLI already stores — nothing new to authenticate.

| Provider | Authentication owner | Telemetry source | Windows |
|----------|----------------------|------------------|---------|
| Claude | Claude Code | `api.anthropic.com/api/oauth/usage` | 5-hour + weekly + model-specific weekly |
| Codex | Codex CLI | app-server `account/rateLimits/read` | primary/secondary |
| Grok | Grok CLI | `cli-chat-proxy.grok.com/v1/billing?format=credits` | weekly SuperGrok pool |
| Grok (team) | management key | `management-api.x.ai/v1/billing/teams/{id}` | weekly spend vs credit total |

Claude Code and Codex remain responsible for authentication; this project only reads the
tokens those CLIs already store. Grok is the exception twice over: an expired access token
is refreshed the same way starting `grok` is (no re-login), and **team** accounts need an
xAI management key of their own (see [Grok team accounts](#grok-team-accounts)). Parsing is
defensive and any provider failure is isolated rather than crashing the collection.

## Read-only telemetry API

The JSON command emits all provider windows under the versioned schema:

```bash
ai-model-usage --json
python3 -m ai_model_usage --json
```

Python consumers receive the same typed `Telemetry` objects directly:

```python
from ai_model_usage import collect_telemetry

result = collect_telemetry()
for snapshot in result.telemetry:
    headline = snapshot.most_constrained()
```

`result.errors` contains isolated provider failures. No JSON serialization/parsing happens
inside the Python API.

## Configuration

First run writes `~/.config/ai-usage-indicator/config.toml` (perms `0600`). Edit it to change
the refresh interval or add/remove providers. Supported `type`s: `claude`, `codex`, `grok`, `mock`.

### Grok team accounts

Grok's CLI billing endpoint reports **no usage at all** for Team principals — just a billing
period with zeroed counters. The real numbers live behind the xAI Management API, the same
source console.x.ai renders, which needs a management key; the Grok CLI's OAuth token cannot
reach it (its scopes stop at `grok-cli`/`api`/`conversations`/`workspaces`).

Create one at **console.x.ai → Settings → Management Keys**, then:

```toml
[[providers]]
id = "grok"
type = "grok"
display_name = "Grok"
management_key = "xai-..."   # or export XAI_MANAGEMENT_KEY
# team_id = "..."            # optional; defaults to the team in ~/.grok/auth.json
```

The bar then shows real spend for the current billing period against the team's credit
total, both read live — nothing hardcoded. This key is the **only** credential the project
stores itself; keep the config at `0600` (its default).

Without a key, a team account falls back to summing local `~/.grok/sessions/**/usage.json`
ledgers against `allowance_usd` (for example `150`). Treat that as a floor, not the truth:
it cannot see Grok web-app usage, other machines, or sessions the CLI never wrote a ledger
for — in practice it read ~25% low.

## Development

```bash
# run the backend once, without installing:
PYTHONPATH=src python3 -m ai_usage_indicator --once   # writes state.json and exits
PYTHONPATH=src python3 -m ai_usage_indicator          # run the refresh loop
PYTHONPATH=src python3 -m ai_model_usage --json       # read-only full telemetry
```
