#!/usr/bin/env bash
# Windy Jarvis launcher — sets up the Wayland "hands" environment, then starts the
# always-on voice agent.
set -euo pipefail
cd "$(dirname "$0")"

# 1) Point ydotool at this user's socket (Wayland input injection).
export YDOTOOL_SOCKET="${YDOTOOL_SOCKET:-/run/user/$(id -u)/.ydotool_socket}"
if [ ! -S "$YDOTOOL_SOCKET" ]; then
  echo "Starting ydotoold (user input daemon)…"
  (ydotoold --socket-path="$YDOTOOL_SOCKET" --socket-perm=0600 &) 2>/dev/null
  sleep 1
fi

# 2) Turn on toolkit accessibility so apps expose their UI trees to AT-SPI.
gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null || true

# 3) Load .env if present (also handled inside config.py).
[ -f .env ] && set -a && . ./.env && set +a || true

exec python3 jarvis.py "$@"
