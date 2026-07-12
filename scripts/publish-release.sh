#!/usr/bin/env bash
# Sign + publish a Windy Talk release to the contract-pinned update channel
# (GitHub Releases of this repo). The app's check_for_update/apply_update find
# the newest non-prerelease Release and verify each artifact against the
# embedded public key, so every artifact MUST ship with its detached .sig.
#
#   WINDYTALK_UPDATE_KEY=~/windytalk-update-key/windytalk-update-private.pem \
#     ./scripts/publish-release.sh <version> <artifact...>
#
#   e.g. ./scripts/publish-release.sh 1.1.0 \
#          dist/windytalk-1.1.0-win-x64.exe \
#          dist/windytalk-1.1.0-mac-universal.dmg \
#          dist/windytalk-1.1.0-linux-x86_64.AppImage
#
# Enforces the ONE artifact-naming rule (ghsource.ts artifactAssetName):
#   windytalk-<version>-{win-x64.exe|mac-universal.dmg|linux-x86_64.AppImage}
# Signs each artifact (Ed25519 detached), VERIFIES each signature against the
# private key's own public half (fail closed), then creates release v<version>.
# Publish drafts/prereleases by hand if needed — the app ignores them.
set -euo pipefail
cd "$(dirname "$0")/.."

die() { echo "PUBLISH FAIL: $*" >&2; exit 1; }

VERSION="${1:-}"; shift || true
[ -n "$VERSION" ] && [ $# -ge 1 ] || die "usage: publish-release.sh <version> <artifact...>"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must be bare semver (got: $VERSION)"
KEY="${WINDYTALK_UPDATE_KEY:-}"
[ -f "$KEY" ] || die "WINDYTALK_UPDATE_KEY must point at the private pem"

PUBTMP="$(mktemp)"; trap 'rm -f "$PUBTMP"' EXIT
openssl pkey -in "$KEY" -pubout -out "$PUBTMP"

ASSETS=()
for f in "$@"; do
  [ -f "$f" ] || die "no such artifact: $f"
  base="$(basename "$f")"
  case "$base" in
    "windytalk-$VERSION-win-x64.exe" | \
    "windytalk-$VERSION-mac-universal.dmg" | \
    "windytalk-$VERSION-linux-x86_64.AppImage") ;;
    *) die "artifact name breaks the naming rule the app looks for: $base" ;;
  esac
  openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$f" -out "$f.sig"
  # fail CLOSED: prove the sig verifies before anything is uploaded
  openssl pkeyutl -verify -pubin -inkey "$PUBTMP" -rawin -in "$f" -sigfile "$f.sig" >/dev/null \
    || die "self-verification failed for $base"
  echo "signed + verified: $base"
  ASSETS+=("$f" "$f.sig")
done

gh release create "v$VERSION" "${ASSETS[@]}" \
  --title "Windy Talk $VERSION" \
  --notes "Windy Talk $VERSION. Artifacts are Ed25519-signed (detached .sig); the app verifies before staging and only ever installs the channel head."
echo "PUBLISHED v$VERSION — installs will see it on their next check_for_update."
