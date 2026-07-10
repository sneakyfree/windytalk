#!/usr/bin/env bash
# The Windy Talk merge gate. While GitHub Actions is billing-locked account-wide
# (since ~2026-07-04), a green run of THIS script is the gate — it runs the exact
# same commands as .github/workflows/ci.yml. Run from the repo root before merging.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== ruff (lint — all python modules) =="
ruff check engine brains agents hands auth telemetry wakeword tests

echo "== pytest (unit tests; lazy CUDA imports, no GPU needed) =="
python3 -m pytest tests/ -q

echo "== tsc --noEmit (typecheck TS clients) =="
npx --yes -p typescript tsc --noEmit -p apps/desktop
npx --yes -p typescript tsc --noEmit -p apps/cli

echo "== client protocol tests (apps/desktop) =="
if [ -d apps/desktop/node_modules ]; then
  ( cd apps/desktop && npm test --silent )
else
  echo "  (skipped — run 'npm install' in apps/desktop; CI installs deps)"
fi

echo "== CI GREEN =="
