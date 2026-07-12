#!/usr/bin/env bash
# Assemble the per-OS PAYLOAD electron-builder packs as extraResources —
# the bring-your-own-runtime half of the fat-installer doctrine
# (docs/PACKAGING.md): a frozen private Python + the app's python code + the
# OS tool cocktail. The machine's python is never consulted.
#
#   ./scripts/build-payload.sh linux            (macos/windows variants: P4b)
#
# Python runtime is python-build-standalone, PINNED in
# packaging/python-runtime.lock (url + sha256). First run resolves the newest
# 3.12 install_only build and WRITES the lock; commit it — later builds verify
# against it and fail closed on any mismatch.
#
# NO SILENT CAPS: everything the cocktail manifest marks "bundled" that this
# script does not yet pack is listed loudly in payload-manifest.json
# (not_yet_bundled) and on stdout.
set -euo pipefail
cd "$(dirname "$0")/.."

OS="${1:-linux}"
[ "$OS" = "linux" ] || { echo "only linux is implemented (P4a); mac/win land in P4b"; exit 1; }

LOCK="packaging/python-runtime.lock"
DEST="apps/desktop/payload/current"
PY_DIRS=(hands engine brains server auth telemetry wakeword agents contracts)
CORE_PKGS=(numpy websockets) # cocktail common.json profile=core

die() { echo "PAYLOAD FAIL: $*" >&2; exit 1; }

# --- 1. frozen python (pinned) ------------------------------------------------
if [ ! -f "$LOCK" ]; then
  echo "no $LOCK — resolving newest cpython-3.12 install_only x86_64-linux-gnu (will pin)"
  JSON="$(curl -fsSL --retry 3 https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest)"
  URL="$(echo "$JSON" | python3 -c '
import json,sys
rel=json.load(sys.stdin)
for a in rel["assets"]:
    n=a["name"]
    if n.startswith("cpython-3.12.") and n.endswith("x86_64-unknown-linux-gnu-install_only.tar.gz"):
        print(a["browser_download_url"]); break
')"
  [ -n "$URL" ] || die "could not resolve a python-build-standalone 3.12 asset"
  curl -fsSL --retry 3 -o /tmp/pbs.tar.gz "$URL"
  SHA="$(sha256sum /tmp/pbs.tar.gz | cut -d' ' -f1)"
  printf 'url=%s\nsha256=%s\n' "$URL" "$SHA" > "$LOCK"
  echo "PINNED: $URL"
else
  URL="$(grep '^url=' "$LOCK" | cut -d= -f2-)"
  SHA="$(grep '^sha256=' "$LOCK" | cut -d= -f2-)"
  [ -f /tmp/pbs.tar.gz ] && [ "$(sha256sum /tmp/pbs.tar.gz | cut -d' ' -f1)" = "$SHA" ] \
    || curl -fsSL --retry 3 -o /tmp/pbs.tar.gz "$URL"
  echo "$SHA  /tmp/pbs.tar.gz" | sha256sum -c - >/dev/null || die "python runtime sha256 mismatch vs $LOCK"
fi

rm -rf "$DEST"
mkdir -p "$DEST"
tar -xzf /tmp/pbs.tar.gz -C "$DEST"   # -> $DEST/python/
PYBIN="$DEST/python/bin/python3"
[ -x "$PYBIN" ] || die "frozen python missing at $PYBIN"

# --- 2. core python packages into the FROZEN python ----------------------------
"$PYBIN" -m pip install --quiet --disable-pip-version-check --no-compile "${CORE_PKGS[@]}"

# --- 3. the app's python code ---------------------------------------------------
mkdir -p "$DEST/app-py"
for d in "${PY_DIRS[@]}"; do
  rsync -a --exclude '__pycache__' --exclude 'tests' "$d" "$DEST/app-py/"
done

# --- 4. linux tool cocktail + first-run assets ----------------------------------
mkdir -p "$DEST/tools" "$DEST/firstrun"
PACKED_TOOLS=()
for t in ydotool ydotoold; do
  if [ -x "packaging/linux/out/$t" ]; then
    install -m 0755 "packaging/linux/out/$t" "$DEST/tools/$t"
    PACKED_TOOLS+=("$t")
  fi
done
install -m 0755 packaging/linux/firstrun-linux.sh packaging/linux/build-ydotoold.sh \
                packaging/linux/selftest.py "$DEST/firstrun/"
install -m 0644 packaging/linux/99-windytalk-uinput.rules \
                packaging/linux/windytalk-ydotoold.service.in "$DEST/firstrun/"

# --- 5. honesty manifest: packed vs the cocktail's 'bundled' set -----------------
"$PYBIN" - "$OS" "${PACKED_TOOLS[@]+"${PACKED_TOOLS[@]}"}" <<'PYEOF'
import json, platform, subprocess, sys
os_name, packed = sys.argv[1], set(sys.argv[2:])
cocktail = json.load(open(f"packaging/manifests/{os_name}.json"))
bundled = {t for t, s in cocktail["external_tools"].items() if s["provided_by"] == "bundled"}
gaps = sorted(bundled - packed)
manifest = {
    "payload": f"windytalk-payload.{os_name}.v1",
    "python": subprocess.run(
        ["apps/desktop/payload/current/python/bin/python3", "-V"],
        capture_output=True, text=True).stdout.strip(),
    "built_on_glibc": platform.libc_ver()[1] + " (DEV build box — production builds use the old-glibc container, P4b)",
    "packed_tools": sorted(packed),
    "not_yet_bundled": gaps,
    "note": "not_yet_bundled tools currently come from the user's system when present (the fallback chains handle absence honestly); static bundling of the full set is P4b.",
}
json.dump(manifest, open("apps/desktop/payload/current/payload-manifest.json", "w"), indent=1)
print("PAYLOAD OK —", manifest["python"])
print("packed tools:", ", ".join(sorted(packed)) or "none")
if gaps:
    print("NOT YET BUNDLED (loud, per doctrine):", ", ".join(gaps))
PYEOF
du -sh "$DEST"
