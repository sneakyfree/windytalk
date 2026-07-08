#!/usr/bin/env bash
# Windy Jarvis launcher — sets up the Wayland "hands", ensures the local brain
# tunnel is up, then starts the always-on voice agent.
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
[ -f .env ] && set -a && . ./.env || true
set +a 2>/dev/null || true

# 4) For the local (Veron-5090) brain, ensure the SSH tunnel to the server is up.
PROVIDER="${JARVIS_PROVIDER:-local}"
case " $* " in *" --provider "*) PROVIDER=$(echo " $* " | sed -n 's/.*--provider \([^ ]*\).*/\1/p');; esac
# Only tunnel when pointed at localhost (LAN/fleet mode). The default is the public
# licensed endpoint, which needs no tunnel — that's what shared copies use.
if [ "$PROVIDER" = "local" ]; then
  case "${JARVIS_LOCAL_URL:-}" in
    *localhost*|*127.0.0.1*)
      VERON_HOST="${WJ_VERON_HOST:-wg-veron}"
      if ! (exec 3<>/dev/tcp/localhost/8765) 2>/dev/null; then
        echo "Opening SSH tunnel to the Veron brain server ($VERON_HOST)…"
        setsid ssh -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
          -N -L 8765:localhost:8765 "$VERON_HOST" >/dev/null 2>&1 &
        for i in $(seq 1 10); do (exec 3<>/dev/tcp/localhost/8765) 2>/dev/null && break; sleep 1; done
      fi ;;
  esac
fi

exec python3 jarvis.py "$@"
