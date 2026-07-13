#!/usr/bin/env bash
# Install the AI Usage Indicator: Python backend (systemd --user service) + GNOME extension.
# Uses a self-contained venv so it works on PEP 668 systems (Ubuntu 24.04+/26.04).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HOME/.local/share/ai-usage-indicator/venv"
BIN_LINK="$HOME/.local/bin/ai-usage-indicator"
EXT_UUID="ai-usage-indicator@matom.ai"
EXT_SRC="$REPO/gnome-extension/$EXT_UUID"
EXT_DST="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

echo "==> Creating venv and installing backend"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$REPO"
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/ai-usage-indicator" "$BIN_LINK"
echo "    backend at $BIN_LINK"

echo "==> Installing systemd --user service"
mkdir -p "$HOME/.config/systemd/user"
cp "$REPO/packaging/ai-usage-indicator.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now ai-usage-indicator.service
echo "    service: $(systemctl --user is-active ai-usage-indicator.service)"

echo "==> Installing GNOME Shell extension"
mkdir -p "$EXT_DST"
cp -r "$EXT_SRC/." "$EXT_DST/"

echo
echo "Backend is running. To show the panel widget:"
echo "  1. Log out and back in   (Wayland can't load a new extension without it)"
echo "  2. gnome-extensions enable $EXT_UUID"
