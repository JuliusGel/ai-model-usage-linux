# AI Usage Indicator

Shows how much of your AI subscription **plan usage** you've consumed across multiple
providers, inline in the **GNOME top bar** — a "battery indicator for your AI plans."

- **Glanceable:** each provider appears as `[initial] [bar] [percent]`, colored by pressure
  (green → amber → red). Both providers visible at once, no clicking required.
- **Details on click:** a popup lists each provider's usage windows, reset times, last
  update, and a Refresh action.
- **Multiple providers** behind a plugin interface. Ships with **Claude** and **Codex**.

## Architecture (hybrid)

Two cooperating pieces:

1. **Backend** (`src/ai_usage_indicator/`, pure-stdlib Python) — a `systemd --user` service
   that reads the OAuth tokens the Claude Code / Codex CLIs already store locally, calls each
   provider's plan-usage endpoint, and writes a small snapshot to
   `~/.cache/ai-usage-indicator/state.json`. No credentials are stored by this project.
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

| Provider | Token source | Endpoint | Windows |
|----------|--------------|----------|---------|
| Claude | `~/.claude/.credentials.json` | `api.anthropic.com/api/oauth/usage` | 5-hour + weekly |
| Codex  | `~/.codex/auth.json` | `chatgpt.com/backend-api/codex/usage` | primary/secondary |

These are the same undocumented endpoints the official clients use; parsing is defensive and
any failure surfaces as an error row rather than crashing. If a token is expired you'll see
`unauthorized — run claude/codex to re-auth`.

### Automatic token refresh (opt-in, experimental)

Set `auto_refresh = true` in the config to have the service refresh an expired token via each
vendor's (undocumented) OAuth endpoint and write the new tokens back to that CLI's credential
file (so the CLI keeps working). **Off by default:** it touches your primary `claude`/`codex`
credentials and the endpoints are unofficial. If you use the CLIs regularly you don't need it —
they keep their own tokens fresh, which this tool reads.

## Configuration

First run writes `~/.config/ai-usage-indicator/config.toml` (perms `0600`). Edit it to change
the refresh interval or add/remove providers. Supported `type`s: `claude`, `codex`, `mock`.

## Development

```bash
# run the backend once, without installing:
PYTHONPATH=src python3 -m ai_usage_indicator --once   # writes state.json and exits
PYTHONPATH=src python3 -m ai_usage_indicator          # run the refresh loop
```
