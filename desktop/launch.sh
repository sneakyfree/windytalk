#!/usr/bin/env bash
# Double-clickable entry point for the Windy Jarvis desktop app.
cd "$(dirname "$(readlink -f "$0")")"
if [ ! -x node_modules/.bin/electron ]; then
  echo "First run: installing Electron…"; npm install --no-fund --no-audit || exit 1
fi
exec ./node_modules/.bin/electron . "$@"
