#!/usr/bin/env bash
# The Windy Talk merge gate. While GitHub Actions is billing-locked account-wide
# (since ~2026-07-04), a green run of THIS script is the gate — it runs the exact
# same commands as .github/workflows/ci.yml. Run from the repo root before merging.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== ruff (lint — all python modules) =="
ruff check engine server brains agents hands auth telemetry wakeword gauntlet tests

echo "== Loom contract validation (ADR-060 — windy-contracts) =="
# A contract edit that breaks doctrine validation must FAIL this gate. Locate a
# windy-contracts checkout (WINDY_CONTRACTS_DIR or the sibling ../windy-contracts)
# and run the Loom validator on every contract manifest. If the checkout is
# absent we WARN loudly and skip — never a silent pass (no-silent-caps doctrine).
ROOT="$(pwd)"
WC="${WINDY_CONTRACTS_DIR:-../windy-contracts}"
if [ -f "$WC/pyproject.toml" ] && command -v uv >/dev/null 2>&1; then
  for c in control.mcp.v1 hands.mcp.v1 engine.mcp.v1; do
    ( cd "$WC" && uv run --quiet python -m loom.validate \
        "$ROOT/contracts/$c.json" ) || {
      echo "  LOOM VALIDATION FAILED for contracts/$c.json — fix before merge"; exit 1; }
  done
else
  echo "  ⚠️  WARNING: windy-contracts checkout not found (set WINDY_CONTRACTS_DIR) —"
  echo "     contract doctrine validation SKIPPED. Run it before merging a contract change."
fi

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
