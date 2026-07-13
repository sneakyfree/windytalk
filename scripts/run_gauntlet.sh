#!/usr/bin/env bash
# Run the Windy Talk gauntlet inside the user's REAL desktop session env.
# Usage: run_gauntlet.sh <json-out> <extra runner args...>
set -u
OUT="$1"; shift
cd /tmp/wt_p5

# Harvest the live session env from the compositor process (works on both
# X11 and Wayland; ssh sessions don't inherit it).
COMP_PID=$(pgrep -u "$(id -u)" gnome-shell | head -1)
if [ -n "${COMP_PID:-}" ]; then
  while IFS= read -r -d '' kv; do
    case "$kv" in
      DISPLAY=*|WAYLAND_DISPLAY=*|XAUTHORITY=*|XDG_RUNTIME_DIR=*|DBUS_SESSION_BUS_ADDRESS=*|XDG_SESSION_TYPE=*)
        export "$kv" ;;
    esac
  done < "/proc/$COMP_PID/environ"
fi
export WINDYTALK_VISION_URL=http://10.10.0.6:11434/v1
export WINDYTALK_VISION_MODEL=windy-locator
export WINDYTALK_PORTAL_TIMEOUT="${WINDYTALK_PORTAL_TIMEOUT:-6}"

exec python3 -m gauntlet.runner --json "$OUT" "$@"
