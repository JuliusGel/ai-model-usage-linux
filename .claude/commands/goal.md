---
description: Project goal & spec — AI subscription usage indicator in the GNOME top bar
argument-hint: "[optional: a slice to work on, e.g. 'add gemini provider' or 'popup styling']"
---

# Goal: AI Subscription Usage Indicator (Ubuntu / GNOME top bar)

A **GNOME top-bar widget** for Ubuntu that shows, **at a glance**, how much of my AI
subscription **plan usage** I've consumed across **multiple providers** — a "battery
indicator for my AI plans."

If `$ARGUMENTS` is given, focus this session on that slice; otherwise read the repo's current
state, summarize it in a line or two, and propose the next concrete step. Prefer small,
verifiable increments.

## Interaction model

- **Glanceable at rest:** each provider shows inline as `[provider icon] [usage bar] [percent]`,
  colored by pressure (green → amber → red). Both/all providers visible at once, no clicking.
- **Details on click:** a popup lists each provider's usage windows, reset times, last update,
  and a Refresh action. Nothing verbose lives in the bar at rest.

## Architecture (hybrid — decided and built)

Tray/AppIndicator can't do rich inline widgets (it's one icon + one label), so the UI is a
**GNOME Shell extension**, fed by a **Python backend**:

1. **Backend** (`src/ai_usage_indicator/`, pure stdlib) — a `systemd --user` service that reads
   the OAuth tokens the provider CLIs already store locally, calls each provider's plan-usage
   endpoint, and writes `~/.cache/ai-usage-indicator/state.json`. This project stores no
   credentials of its own.
2. **Extension** (`gnome-extension/ai-usage-indicator@matom.ai/`) — pure presentation: reads
   `state.json`, renders the panel widget + popup. Never calls an API itself.

Keep the provider logic in Python (adding a provider = a Python change), and keep the extension
dumb. The two sides share the `state.json` contract defined in `state.py` — change both together.

## Providers

A **provider plugin** (`providers/base.py`) exposes `id`, `display_name`, `authenticate()`, and
`fetch_usage() -> UsageRecord`. Register new ones in `providers/__init__.py` and drop an icon at
`gnome-extension/.../icons/<id>.svg`. Provider-specific breakage must stay contained
(`safe_fetch()` turns any failure into an error row, never a crash).

Built: **Claude** (`api.anthropic.com/api/oauth/usage`, 5h + weekly windows) and **Codex**
(`chatgpt.com/backend-api/codex/usage`). Both reuse the CLI's local token. Endpoints are
undocumented — parse defensively and note assumptions.

## Working notes / gotchas

- **Wayland can't hot-reload extension code.** A changed `extension.js` needs a new login
  session (or interactive Looking Glass). `state.json` changes ARE picked up live. Verify
  backend changes by running the service; verify extension logic by code review + a relogin,
  or check load state with `gnome-extensions info` and the shell journal
  (`journalctl --user -u org.gnome.Shell@ubuntu.service`).
- **Never commit credentials**; tokens stay in the CLIs' own files (`~/.claude`, `~/.codex`).
- **Token refresh** is opt-in (`auto_refresh` in config, default off) because it writes to the
  CLIs' primary credential files via unofficial endpoints. Don't enable it by default.
- Install/enable: `./install.sh`, then relogin + `gnome-extensions enable ai-usage-indicator@matom.ai`.

## How to work this goal

1. Read the repo; summarize current state.
2. Build in vertical slices; after each backend change, run the service and confirm `state.json`.
3. For extension changes, deploy to `~/.local/share/gnome-shell/extensions/…` and be explicit
   that a relogin is needed to see them.
4. Report: current state, what's next, and any decisions you need from me.
