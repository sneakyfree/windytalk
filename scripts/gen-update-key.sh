#!/usr/bin/env bash
# Generate the Windy Talk update-signing keypair (Ed25519) — GRANT'S one-time
# real-world action (contract self_update.source). This is a real cryptographic
# trust root: whoever holds the PRIVATE half can push code to every install.
#
#   ./scripts/gen-update-key.sh [output-dir]     (default: ~/windytalk-update-key)
#
# Then:
#   1. Paste the PUBLIC pem printed below into EMBEDDED_UPDATE_PUBLIC_KEY in
#      apps/desktop/electron/control/update-key.ts (one PR; nothing else changes).
#   2. Keep the PRIVATE pem OFFLINE (password manager / offline disk). Never in
#      the repo, never on a build box unattended.
#   3. Publish releases with scripts/publish-release.sh (it signs with this key).
set -euo pipefail
OUT="${1:-$HOME/windytalk-update-key}"
PRIV="$OUT/windytalk-update-private.pem"
PUB="$OUT/windytalk-update-public.pem"

[ -e "$PRIV" ] && { echo "REFUSING: $PRIV already exists (never overwrite a trust root)"; exit 1; }
mkdir -p "$OUT"
chmod 700 "$OUT"
openssl genpkey -algorithm ed25519 -out "$PRIV"
chmod 600 "$PRIV"
openssl pkey -in "$PRIV" -pubout -out "$PUB"

echo
echo "PRIVATE key (guard with your life):  $PRIV"
echo "PUBLIC  key (embed in the app):      $PUB"
echo
echo "---- paste this into update-key.ts EMBEDDED_UPDATE_PUBLIC_KEY ----"
cat "$PUB"
