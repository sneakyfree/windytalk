#!/usr/bin/env bash
# Build the bundleable Linux tools against the cocktail's OLD-glibc floor
# (ubuntu:20.04 = glibc 2.31, packaging/manifests/linux.json) so the shipped
# binaries run on 2020-era distros and everything newer. Dev-box builds
# (new glibc) must never ship — this container is the production provenance.
#
#   ./packaging/linux/build-tools-container.sh     -> packaging/linux/out/
#
# TWO-TIER floor, by dependency reality (audited, not assumed):
#   Tier A (ubuntu:20.04, glibc 2.31) — ydotool + ydotoold. The GNOME/KDE-
#     Wayland input path (apt ships no daemon). These are what MATTERS: the
#     priority target is GNOME, where ydotool is the only thing that types.
#     Measured floor is GLIBC 2.3-2.4 — runs on ~any Linux of the last 15y.
#   Tier B (ubuntu:22.04, glibc 2.35) — wtype + grim. wlroots-only rungs
#     (both fail on GNOME by design; the chain pivots). wtype needs
#     libxkbcommon>=1.0 (xkb_utf32_to_keysym), absent on the 2.31 floor — so
#     they carry a 2022 floor. Fine: wlroots users run modern compositors,
#     never a 2020 distro.
# Still not bundled (loud in payload-manifest.json): xdotool, scrot,
# gnome-screenshot, xdg-open (X11/GNOME rungs; system-present on their desktops).
set -euo pipefail
cd "$(dirname "$0")"
ENGINE="${CONTAINER_ENGINE:-podman}"
mkdir -p out

echo "===== Tier A: ydotool + ydotoold on ubuntu:20.04 (glibc 2.31 floor) ====="
"$ENGINE" run --rm -v "$PWD/out:/out:Z" ubuntu:20.04 /bin/bash -ec '
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
apt-get install -qy --no-install-recommends >/dev/null \
  git ca-certificates make g++ gcc libc6-dev python3-pip scdoc
# ydotool master needs cmake >= 3.22; 20.04 apt has 3.16 — pip ships a modern
# cmake that still TARGETS the old glibc via the 20.04 toolchain (tool != ABI).
pip3 install -q cmake
export PATH="/usr/local/bin:$PATH"
git clone -q --depth 1 --recurse-submodules https://github.com/ReimuNotMoe/ydotool /s/ydotool
cmake -S /s/ydotool -B /b/ydotool -DCMAKE_BUILD_TYPE=Release >/dev/null
make -C /b/ydotool -j"$(nproc)" ydotool ydotoold >/dev/null 2>&1
install -m 0755 /b/ydotool/ydotool /b/ydotool/ydotoold /out/
'

echo "===== Tier B: wtype + grim on ubuntu:22.04 (glibc 2.35 floor) ====="
"$ENGINE" run --rm -v "$PWD/out:/out:Z" ubuntu:22.04 /bin/bash -ec '
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
apt-get install -qy --no-install-recommends >/dev/null \
  git ca-certificates make gcc libc6-dev pkg-config meson ninja-build \
  wayland-protocols libwayland-dev libxkbcommon-dev libpixman-1-dev \
  libpng-dev libjpeg-dev scdoc
git clone -q --depth 1 https://github.com/atx/wtype /s/wtype
meson setup /b/wtype /s/wtype >/dev/null && ninja -C /b/wtype >/dev/null 2>&1
install -m 0755 /b/wtype/wtype /out/
git clone -q --depth 1 --branch v1.4.0 https://github.com/emersion/grim /s/grim
meson setup /b/grim /s/grim -Djpeg=enabled >/dev/null && ninja -C /b/grim >/dev/null 2>&1
install -m 0755 /b/grim/grim /out/
'

echo "== glibc floor audit (Tier A <= 2.31 by construction; Tier B <= 2.35) =="
for f in ydotool ydotoold wtype grim; do
  [ -f "out/$f" ] || { echo "  $f: MISSING"; continue; }
  MAX=$(objdump -T "out/$f" 2>/dev/null | grep -oE "GLIBC_[0-9.]+" | sort -Vu | tail -1)
  echo "  $f: $MAX"
done
echo "BUILT: $(ls out/)"
