#!/usr/bin/env bash
# One-command Windy Jarvis client install for a fresh Linux desktop (GNOME).
# Usage: scripts/install-client.sh WINDY-XXXXXX
set -e
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
KEY="${1:-}"
ENDPOINT="${JARVIS_LOCAL_URL:-wss://jarvis.thewindstorm.uk}"

if [ -z "$KEY" ]; then
  read -rp "Enter your Windy Jarvis license key (WINDY-XXXXXX): " KEY
fi
echo "Installing Windy Jarvis → endpoint $ENDPOINT"

# 1) System dependencies (voice + hands + Electron). Only add node/npm if missing —
# on boxes that already have node (e.g. nodesource), `apt install npm` conflicts.
NODE_PKgs=""
command -v npm >/dev/null 2>&1 || NODE_PKgs="nodejs npm"
if command -v apt-get >/dev/null; then
  sudo apt-get update -q
  sudo apt-get install -y ydotool xdotool scrot flameshot python3-gi gir1.2-atspi-2.0 \
    python3-pip portaudio19-dev python3-dev at-spi2-core $NODE_PKgs
elif command -v dnf >/dev/null; then
  sudo dnf install -y ydotool xdotool scrot flameshot python3-gobject at-spi2-core \
    python3-pip portaudio-devel python3-devel $NODE_PKgs
else
  echo "Unsupported distro — install manually: ydotool flameshot python3-gi at-spi2 portaudio nodejs"; exit 1
fi
command -v npm >/dev/null 2>&1 || { echo "npm still missing — install Node.js manually, then rerun"; exit 1; }

# 2) Python deps (--break-system-packages: Ubuntu 24.04+ PEP 668; still user-scoped)
PIPFLAGS="--user --quiet"
python3 -m pip install --help 2>/dev/null | grep -q break-system-packages && PIPFLAGS="$PIPFLAGS --break-system-packages"
python3 -m pip install $PIPFLAGS aiohttp numpy pyaudio openwakeword onnxruntime

# 3) ydotool daemon (Wayland input) + uinput permission
sudo systemctl enable --now ydotool 2>/dev/null || true
sudo bash -c 'echo "KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\"" > /etc/udev/rules.d/99-uinput.rules' 2>/dev/null || true
sudo usermod -aG input "$USER" 2>/dev/null || true

# 4) Electron for the face app
( cd "$REPO/desktop" && npm install --no-fund --no-audit )

# 5) Config: license + endpoint
cat > "$REPO/.env" <<EOF
JARVIS_PROVIDER=local
JARVIS_LICENSE=$KEY
JARVIS_LOCAL_URL=$ENDPOINT
JARVIS_WAKE=1
EOF

# 6) App-grid launcher
bash "$REPO/scripts/install-launcher.sh"

echo
echo "Done. Launch 'Windy Jarvis' from your app grid (or: gtk-launch windy-jarvis)."
echo "Say 'Hey Jarvis' then a command. If you were added to the 'input' group, log out/in once."
