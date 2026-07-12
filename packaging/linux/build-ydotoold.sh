#!/usr/bin/env bash
# Build ydotool + ydotoold from source into ./out/. This is the BUILD-TIME
# recipe used to produce the binaries the installer bundles — Ubuntu/Debian's
# `apt install ydotool` ships the CLIENT ONLY (no daemon), so the shipped app
# must carry its own (packaging finding, OC3 2026-07-11). firstrun-linux.sh
# also uses this as a fallback when no prebuilt binary is supplied.
#
# Deps: git, cmake, make, a C++ compiler, libevdev headers are NOT needed —
# ydotool vendors uInputPlus/libevdevPlus via git submodules.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/out}"
SRC="$(mktemp -d)"
trap 'rm -rf "$SRC"' EXIT

git clone --depth 1 --recurse-submodules https://github.com/ReimuNotMoe/ydotool "$SRC"
cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release
make -C "$SRC/build" -j"$(nproc)" ydotool ydotoold

mkdir -p "$OUT"
install -m 0755 "$SRC/build/ydotool" "$SRC/build/ydotoold" "$OUT/"
echo "built: $OUT/ydotool $OUT/ydotoold"
