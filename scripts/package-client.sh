#!/usr/bin/env bash
# Build a distributable Windy Jarvis CLIENT tarball. Send it + a license key to a
# user; the server/ code (Grant's brain) is intentionally left out.
set -e
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
OUT="${1:-$HOME/windy-jarvis-client.tar.gz}"

STAGE="$(mktemp -d)/windy-jarvis"
mkdir -p "$STAGE"
cp -r "$REPO"/*.py "$REPO/providers" "$REPO/desktop" "$REPO/scripts" \
      "$REPO/run.sh" "$REPO/requirements.txt" "$REPO/README.md" "$STAGE"/
# strip build/dev/secret artifacts
rm -rf "$STAGE/desktop/node_modules" "$STAGE/desktop/package-lock.json" \
       "$STAGE"/**/__pycache__ "$STAGE/__pycache__" "$STAGE/.env" 2>/dev/null || true
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true

tar -czf "$OUT" -C "$(dirname "$STAGE")" windy-jarvis
rm -rf "$(dirname "$STAGE")"
echo "Client package: $OUT ($(du -h "$OUT" | cut -f1))"
echo
echo "Give it to a user with their key. They run:"
echo "  tar xzf windy-jarvis-client.tar.gz && cd windy-jarvis && scripts/install-client.sh WINDY-XXXXXX"
