#!/usr/bin/env bash
# The Windy Talk control-surface CHAOS harness (control.mcp.v1 §Gap 5) — the
# measurement that makes "steamroller proof" a number you defend, not a hope.
#
# Two layers:
#   1. The fault-injection SUITE (test/chaos.test.ts): every fault class from
#      the design's Gap 5, each with an asserted recovery-time BUDGET, run
#      against the real modules. Part of the normal gate (scripts/ci.sh).
#   2. This script additionally drives the REAL Electron app end to end under
#      real SIGKILL / SIGSTOP when a display is available — the live proof that
#      the whole stack (heartbeat + watcher + :8782 + relaunch) recovers a
#      genuinely-killed app within the 45 s budget on THIS box.
#
# No silent caps: fault classes that need a live voice engine (Grant's 5090 /
# the cloud) are LISTED as explicitly deferred, never quietly skipped.
set -uo pipefail
cd "$(dirname "$0")/.."
DESK=apps/desktop
FAILED=0

echo "== chaos layer 1: the fault-injection suite (asserted recovery budgets) =="
( cd "$DESK" && npm run build --silent && node --test dist/test/chaos.test.js ) || FAILED=1

echo
echo "== chaos layer 2: live real-app SIGKILL -> relaunch <=45s =="
if [ -z "${DISPLAY:-}" ] && [ "$(uname)" != "Darwin" ]; then
  echo "  SKIP (no DISPLAY): run on a machine with a desktop session to exercise"
  echo "        the real Electron end-to-end resurrection cycle. NOT counted as pass."
else
  CTL="$(mktemp -d)"
  export WINDYTALK_CONTROL_DIR="$CTL"
  # This driver runs the watcher under system `node` (not the app's electron
  # binary), so the production "relaunch cmd must be the app binary" pin would
  # refuse the electron relaunch. Opt into the dev/test escape explicitly.
  export WINDYTALK_ALLOW_FOREIGN_RELAUNCH=1
  # Write the relaunch spec the watcher uses (dev launch shape).
  APP_ABS="$(cd "$DESK" && pwd)"
  cat > "$CTL/resurrection.json" <<JSON
{"launch":{"cmd":"$APP_ABS/node_modules/.bin/electron","args":["$APP_ABS","--no-sandbox","--disable-gpu"],"cwd":"$APP_ABS","env":{"WINDYTALK_CONTROL_DIR":"$CTL","TMPDIR":"/tmp"}}}
JSON
  ( cd "$DESK" && TMPDIR=/tmp setsid nohup ./node_modules/.bin/electron . --no-sandbox --disable-gpu > "$CTL/boot.log" 2>&1 & )
  sleep 8
  if [ ! -f "$CTL/heartbeat" ]; then
    echo "  FAIL: the app never wrote a heartbeat"; FAILED=1
  else
    APP_PID="$(node -e "console.log(JSON.parse(require('fs').readFileSync('$CTL/heartbeat')).pid)")"
    echo "  app up (pid $APP_PID); SIGKILL, then drive the 15 s watcher cadence"
    kill -9 "$APP_PID"
    KILL_T=$(date +%s)
    RECOVERED=0
    for i in 1 2 3; do
      sleep 15
      WINDYTALK_CONTROL_DIR="$CTL" node "$DESK/dist/electron/resurrection/watcher.js" --once >> "$CTL/watch.log" 2>&1
      sleep 3
      if [ -f "$CTL/heartbeat" ]; then
        NEW_PID="$(node -e "try{console.log(JSON.parse(require('fs').readFileSync('$CTL/heartbeat')).pid)}catch(e){console.log('')}" 2>/dev/null)"
        if [ -n "$NEW_PID" ] && [ "$NEW_PID" != "$APP_PID" ] && kill -0 "$NEW_PID" 2>/dev/null; then
          ELAPSED=$(( $(date +%s) - KILL_T ))
          echo "  RECOVERED: new app pid $NEW_PID after ${ELAPSED}s (budget 45s)"
          [ "$ELAPSED" -le 45 ] && RECOVERED=1 || { echo "  FAIL: over budget"; FAILED=1; }
          kill -9 "$NEW_PID" 2>/dev/null
          break
        fi
      fi
    done
    [ "$RECOVERED" -eq 1 ] || { echo "  FAIL: app did not recover in budget"; FAILED=1; }
  fi
  # cleanup any stragglers by heartbeat pid only (never process-name scan)
  [ -f "$CTL/heartbeat" ] && kill -9 "$(node -e "try{console.log(JSON.parse(require('fs').readFileSync('$CTL/heartbeat')).pid)}catch(e){}" 2>/dev/null)" 2>/dev/null
  rm -rf "$CTL"
fi

echo
echo "== deferred fault classes (need a live voice engine — run under Grant's engine) =="
echo "  - engine kill mid-turn / brain endpoint 500 or hang / network pull"
echo "  - audio-device yank+swap mid-session"
echo "  (Layer 1's reconnect behavior under these is asserted in test/chaos.test.ts"
echo "   via injected status; the live-engine end-to-end is Grant's to run.)"

echo
if [ "$FAILED" -eq 0 ]; then
  echo "== CHAOS GREEN — steamroller proof (within the exercised fault classes) =="
else
  echo "== CHAOS FAILED =="
  exit 1
fi
