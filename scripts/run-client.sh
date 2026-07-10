#!/usr/bin/env bash
# Windy Talk — local audio-test launcher (run in a REAL desktop session, not the
# Claude Code sandbox). Opens the tunnel to the 5090 engine, starts the local
# hands surface, then launches the Electron client. Ctrl-C tears it all down.
#
# Prereq: the engine is running on Veron (`~/windytalk-engine/run-engine.sh`).
set -euo pipefail
cd "$(dirname "$0")/.."

cleanup() { kill "${TUNNEL_PID:-}" "${HANDS_PID:-}" 2>/dev/null || true; }
trap cleanup EXIT

# One shared secret for the hands port — the client (Electron main) presents it,
# the surface requires it. Without it a webpage could drive the desktop.
export WINDYTALK_HANDS_TOKEN="${WINDYTALK_HANDS_TOKEN:-$(head -c24 /dev/urandom | xxd -p)}"

echo "[1/3] tunnel Windy0:8788 → Veron 5090…"
ssh -N -L 8788:localhost:8788 wg-veron &
TUNNEL_PID=$!
sleep 2

echo "[2/3] local hands surface on :8781 (token-gated)…"
WINDYTALK_HANDS_AUTOAPPROVE="${WINDYTALK_HANDS_AUTOAPPROVE:-0}" python3 -m hands &
HANDS_PID=$!
sleep 1

echo "[3/3] launching the Windy Talk client — say something!"
cd apps/desktop
[ -d node_modules ] || npm install --no-audit --no-fund
npm start
