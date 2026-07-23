# AGENTS.md

Orientation for coding agents working in this repo. Read this first to get the shape of the
project, then dive into the specific file you need.

## What this is

A **GNOME top-bar widget** for Ubuntu that shows, at a glance, how much of your AI
subscription **plan usage** you've consumed across multiple providers — a "battery indicator
for your AI plans." Each provider renders inline as `[icon] [bar] [percent]`, colored by
pressure (green → amber → red); clicking opens a details popup.

## Architecture (hybrid — decided and built)

Tray/AppIndicator can't do rich inline widgets (one icon + one label only), so the UI is a
**GNOME Shell extension** fed by a **Python backend**. They communicate through one file.

```
┌─────────────────────────┐     writes      ┌──────────────────────┐     reads      ┌───────────────────┐
│ Python backend           │ ──────────────▶ │ ~/.cache/ai-usage-    │ ─────────────▶ │ GNOME Shell        │
│ (systemd --user service) │   state.json    │ indicator/state.json  │                │ extension (JS)     │
│ reads provider CLI tokens│                 │ (the contract)        │                │ pure presentation  │
└─────────────────────────┘                 └──────────────────────┘                └───────────────────┘
```

- **Backend** (`src/ai_usage_indicator/`, pure stdlib) — reads the OAuth tokens the provider
  CLIs already store locally, calls each provider's plan-usage endpoint, writes `state.json`.
  Stores no credentials of its own.
- **Extension** (`gnome-extension/ai-usage-indicator@matom.ai/`) — pure presentation. Reads
  `state.json`, renders the panel widget + popup. **Never calls an API itself.**

The two sides share the `state.json` contract defined in `state.py` — **change both together.**

## Layout

```
src/ai_usage_indicator/          Python backend (stdlib only: urllib, tomllib)
  __main__.py                    Entry point / service loop. `--once` = fetch, write, exit.
  config.py                      TOML config in ~/.config/ai-usage-indicator/ (0600). Defaults.
  state.py                       THE CONTRACT: UsageRecord → state.json dict. Atomic write.
  usage.py                       UsageRecord dataclass + Pressure enum + thresholds.
  net.py                         Tiny stdlib HTTP helper (get_json / post_json, HttpError).
  providers/
    base.py                      Provider ABC: authenticate(), fetch_usage(), safe_fetch().
    __init__.py                  Registry: build_provider() maps config `type` → class.
    claude.py                    Claude: ~/.claude/.credentials.json, 5h + weekly windows.
    codex.py                     Codex: ~/.codex/auth.json, primary/secondary windows.
    mock.py                      Fake provider for testing (config type = "mock").

gnome-extension/ai-usage-indicator@matom.ai/
  extension.js                   Panel widget + popup. Reads state.json, renders bars.
  metadata.json                  UUID, shell-version (48–50), version.
  stylesheet.css                 Bar / pressure-color styling.
  icons/<id>.svg                 One SVG per provider id (claude.svg, codex.svg).

packaging/ai-usage-indicator.service   systemd --user unit.
install.sh                              venv + backend + service + copy extension.
pyproject.toml                          Package metadata; entry point ai-usage-indicator.
README.md                               User-facing docs.
```

## The state.json contract (`state.py`)

```jsonc
{
  "updated_at": 1690000000,          // unix seconds
  "providers": [
    {
      "id": "claude",
      "display_name": "Claude",
      "percent": 42,                 // int 0-100, or null if unknown
      "pressure": "normal",          // normal | warning | near-limit | unknown
      "label": "...",                // compact one-liner
      "detail": "...",               // popup line
      "reset_text": "Resets in 3 h", // or null
      "error": null                  // string when the fetch failed
    }
  ]
}
```

Pressure thresholds live in `usage.py`: `WARNING_AT = 0.75`, `NEAR_LIMIT_AT = 0.90`.

## Adding a provider

1. Implement `Provider` in `providers/<id>.py` (`id`, `display_name`, `authenticate()`,
   `fetch_usage() -> UsageRecord`).
2. Register it in `providers/__init__.py` (`build_provider`).
3. Drop `gnome-extension/.../icons/<id>.svg`.
4. Add a `[[providers]]` block (users edit their own config; the default is in `config.py`).

Keep failures contained: `safe_fetch()` turns any exception into a `UsageRecord(error=...)`
row — a broken provider must never crash the service or the tray.

## Running & verifying

```bash
# Backend, without installing (writes state.json and exits):
PYTHONPATH=src python3 -m ai_usage_indicator --once
PYTHONPATH=src python3 -m ai_usage_indicator          # refresh loop

# Installed service:
systemctl --user status ai-usage-indicator.service
journalctl --user -u ai-usage-indicator.service

# Extension load state / shell logs:
gnome-extensions info ai-usage-indicator@matom.ai
journalctl --user -u org.gnome.Shell@ubuntu.service
```

Install / enable: `./install.sh`, then **relogin** + `gnome-extensions enable ai-usage-indicator@matom.ai`.

## Gotchas

- **Wayland can't hot-reload extension code.** A changed `extension.js` needs a new login
  session (or interactive Looking Glass). **`state.json` changes ARE picked up live** — so
  verify backend changes by running the service and checking `state.json`; verify extension
  logic by code review + a relogin.
- **Never commit credentials.** Tokens stay in the CLIs' own files (`~/.claude`, `~/.codex`);
  this project reads them and stores nothing.
- **Token refresh is opt-in** (`auto_refresh` in config, default off) — it writes back to the
  CLIs' primary credential files via unofficial endpoints. Don't enable it by default.
- Provider endpoints are **undocumented** — parse defensively and note assumptions.
- Backend is **stdlib-only** by design (`dependencies = []`); don't add third-party Python deps.
- Config, cache, and credentials all live **outside the repo** (XDG dirs).
