#!/usr/bin/env bash
# Assemble the per-OS PAYLOAD electron-builder packs as extraResources —
# the bring-your-own-runtime half of the fat-installer doctrine
# (docs/PACKAGING.md): a frozen private Python + the app's python code + the
# OS tool cocktail. The machine's python is never consulted.
#
#   ./scripts/build-payload.sh linux|windows|macos
#
# Run it for the TARGET OS immediately before `electron-builder --<os>` — the
# payload lands in apps/desktop/payload/current, which the builder packs.
#
# Python runtime = python-build-standalone, PINNED per OS in
# packaging/python-runtime-<os>.lock (url + sha256). First run resolves the
# asset FROM THE SAME UPSTREAM RELEASE as any existing lock (release
# coherence) and writes the pin; commit it — later builds verify fail-closed.
# macOS ships BOTH arch runtimes (python-x64 + python-arm64) because upstream
# publishes no universal2 python; the hands launcher picks by arch at runtime.
#
# NO SILENT CAPS: everything the cocktail manifest marks "bundled" that this
# script does not pack is listed loudly in payload-manifest.json.
set -euo pipefail
cd "$(dirname "$0")/.."

OS="${1:-linux}"
case "$OS" in linux|windows|macos) ;; *) echo "usage: build-payload.sh linux|windows|macos"; exit 1 ;; esac

DEST="apps/desktop/payload/current"
PY_DIRS=(hands engine brains server auth telemetry wakeword agents contracts)
CORE_PKGS=(numpy websockets) # cocktail common.json profile=core
PBS_REPO="astral-sh/python-build-standalone"

die() { echo "PAYLOAD FAIL: $*" >&2; exit 1; }

# --- release coherence: reuse the tag any existing lock already pinned -----------
release_tag() {
  local f
  for f in packaging/python-runtime-*.lock; do
    [ -f "$f" ] || continue
    grep '^url=' "$f" | sed -E 's|.*/download/([0-9]+)/.*|\1|' | head -1
    return
  done
}

# fetch_python <lockfile> <asset-suffix> <extract-dir>
fetch_python() {
  local lock="$1" suffix="$2" out="$3" url sha tmp tag
  tmp="/tmp/pbs-$(basename "$lock" .lock)-$suffix.tar.gz"
  if [ ! -f "$lock" ]; then
    tag="$(release_tag)"
    if [ -n "$tag" ]; then
      echo "no $lock — resolving cpython-3.12 $suffix from pinned release $tag"
      url="$(curl -fsSL --retry 3 "https://api.github.com/repos/$PBS_REPO/releases/tags/$tag" \
        | python3 -c "
import json,sys
for a in json.load(sys.stdin)['assets']:
    n=a['name']
    if n.startswith('cpython-3.12.') and n.endswith('$suffix'):
        print(a['browser_download_url']); break
")"
    else
      echo "no locks at all — resolving newest cpython-3.12 $suffix (will pin)"
      url="$(curl -fsSL --retry 3 "https://api.github.com/repos/$PBS_REPO/releases/latest" \
        | python3 -c "
import json,sys
for a in json.load(sys.stdin)['assets']:
    n=a['name']
    if n.startswith('cpython-3.12.') and n.endswith('$suffix'):
        print(a['browser_download_url']); break
")"
    fi
    [ -n "$url" ] || die "could not resolve a python-build-standalone asset ($suffix)"
    curl -fsSL --retry 3 -o "$tmp" "$url"
    sha="$(sha256sum "$tmp" | cut -d' ' -f1)"
    printf 'url=%s\nsha256=%s\n' "$url" "$sha" > "$lock"
    echo "PINNED: $url"
  else
    url="$(grep '^url=' "$lock" | cut -d= -f2-)"
    sha="$(grep '^sha256=' "$lock" | cut -d= -f2-)"
    [ -f "$tmp" ] && [ "$(sha256sum "$tmp" | cut -d' ' -f1)" = "$sha" ] \
      || curl -fsSL --retry 3 -o "$tmp" "$url"
    echo "$sha  $tmp" | sha256sum -c - >/dev/null || die "python sha256 mismatch vs $lock"
  fi
  mkdir -p "$out"
  tar -xzf "$tmp" -C "$out" # extracts python/
}

# unpack_wheels <platform-tag> <site-packages-dir> [pkgs…] — cross-OS pip via
# wheel unzip; defaults to the core cocktail packages.
unpack_wheels() {
  local plat="$1" site="$2" wdir
  shift 2
  local pkgs=("$@")
  [ ${#pkgs[@]} -gt 0 ] || pkgs=("${CORE_PKGS[@]}")
  wdir="$(mktemp -d)"
  python3 -m pip download --quiet --disable-pip-version-check \
    --only-binary=:all: --platform "$plat" --python-version 3.12 \
    --implementation cp -d "$wdir" "${pkgs[@]}" \
    || die "wheel download failed for $plat"
  mkdir -p "$site"
  local w
  for w in "$wdir"/*.whl; do unzip -qo "$w" -d "$site"; done
  rm -rf "$wdir"
}

# fetch_pinned <lockfile> <default-url> <out-file> — sha-pinned single-file fetch
fetch_pinned() {
  local lock="$1" url_default="$2" out="$3" url sha
  if [ ! -f "$lock" ]; then
    curl -fsSL --retry 3 -o "$out" "$url_default"
    printf 'url=%s\nsha256=%s\n' "$url_default" "$(sha256sum "$out" | cut -d' ' -f1)" > "$lock"
    echo "PINNED: $url_default"
  else
    url="$(grep '^url=' "$lock" | cut -d= -f2-)"
    sha="$(grep '^sha256=' "$lock" | cut -d= -f2-)"
    curl -fsSL --retry 3 -o "$out" "$url"
    echo "$sha  $out" | sha256sum -c - >/dev/null || die "sha256 mismatch vs $lock"
  fi
}

rm -rf "$DEST"
mkdir -p "$DEST"
PACKED_TOOLS=()

case "$OS" in
  linux)
    fetch_python packaging/python-runtime-linux.lock \
      "x86_64-unknown-linux-gnu-install_only.tar.gz" "$DEST"
    "$DEST/python/bin/python3" -m pip install --quiet --disable-pip-version-check \
      --no-compile "${CORE_PKGS[@]}"
    ;;
  windows)
    fetch_python packaging/python-runtime-windows.lock \
      "x86_64-pc-windows-msvc-install_only.tar.gz" "$DEST"
    [ -f "$DEST/python/python.exe" ] || die "windows python.exe missing after extract"
    unpack_wheels win_amd64 "$DEST/python/Lib/site-packages"
    ;;
  macos)
    fetch_python packaging/python-runtime-macos-x64.lock \
      "x86_64-apple-darwin-install_only.tar.gz" "$DEST/.x64"
    fetch_python packaging/python-runtime-macos-arm64.lock \
      "aarch64-apple-darwin-install_only.tar.gz" "$DEST/.arm64"
    mv "$DEST/.x64/python" "$DEST/python-x64" && rmdir "$DEST/.x64"
    mv "$DEST/.arm64/python" "$DEST/python-arm64" && rmdir "$DEST/.arm64"
    # core + the native mouse prong (pyobjc ships universal2 wheels)
    unpack_wheels macosx_10_13_x86_64 "$DEST/python-x64/lib/python3.12/site-packages" \
      "${CORE_PKGS[@]}" pyobjc-framework-Quartz
    unpack_wheels macosx_11_0_arm64 "$DEST/python-arm64/lib/python3.12/site-packages" \
      "${CORE_PKGS[@]}" pyobjc-framework-Quartz
    # cliclick: upstream releases a true universal (x86_64+arm64) binary
    mkdir -p "$DEST/tools"
    fetch_pinned packaging/cliclick.lock \
      "https://github.com/BlueM/cliclick/releases/download/5.1/cliclick.zip" \
      /tmp/cliclick.zip
    unzip -qo -j /tmp/cliclick.zip "cliclick/cliclick" -d "$DEST/tools"
    chmod 0755 "$DEST/tools/cliclick"
    PACKED_TOOLS+=(cliclick)
    ;;
esac

# --- the app's python code (identical on every OS) --------------------------------
mkdir -p "$DEST/app-py"
for d in "${PY_DIRS[@]}"; do
  rsync -a --exclude '__pycache__' --exclude 'tests' "$d" "$DEST/app-py/"
done

# --- OS tool cocktail + first-run assets ------------------------------------------
if [ "$OS" = "linux" ]; then
  mkdir -p "$DEST/tools" "$DEST/firstrun"
  for t in ydotool ydotoold wtype grim; do
    if [ -x "packaging/linux/out/$t" ]; then
      install -m 0755 "packaging/linux/out/$t" "$DEST/tools/$t"
      PACKED_TOOLS+=("$t")
    fi
  done
  install -m 0755 packaging/linux/firstrun-linux.sh packaging/linux/build-ydotoold.sh \
                  packaging/linux/selftest.py "$DEST/firstrun/"
  install -m 0644 packaging/linux/99-windytalk-uinput.rules \
                  packaging/linux/windytalk-ydotoold.service.in "$DEST/firstrun/"
fi
# windows: the cocktail is os-builtin (PowerShell/.NET) — nothing to pack.
# macos: cliclick + pyobjc bundling lands with the mac build pass (loud gap below).

# --- honesty manifest: packed vs the cocktail's 'bundled' set ----------------------
python3 - "$OS" "${PACKED_TOOLS[@]+"${PACKED_TOOLS[@]}"}" <<'PYEOF'
import json, platform, sys
os_name, packed = sys.argv[1], set(sys.argv[2:])
cocktail = json.load(open(f"packaging/manifests/{os_name}.json"))
bundled = {t for t, s in cocktail["external_tools"].items() if s["provided_by"] == "bundled"}
gaps = sorted(bundled - packed)
manifest = {
    "payload": f"windytalk-payload.{os_name}.v1",
    "built_on": f"{platform.system()} glibc={platform.libc_ver()[1] or 'n/a'}",
    "packed_tools": sorted(packed),
    "not_yet_bundled": gaps,
    "note": "not_yet_bundled tools come from the user's system when present (fallback chains handle absence honestly); closing the set is tracked in docs/PACKAGING.md.",
}
json.dump(manifest, open("apps/desktop/payload/current/payload-manifest.json", "w"), indent=1)
print(f"PAYLOAD OK ({os_name})")
print("packed tools:", ", ".join(sorted(packed)) or "none (cocktail is os-builtin)")
if gaps:
    print("NOT YET BUNDLED (loud, per doctrine):", ", ".join(gaps))
PYEOF
du -sh "$DEST"
