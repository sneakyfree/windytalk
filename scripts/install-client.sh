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

# 1) System dependencies (voice + hands + Electron)
if command -v apt-get >/dev/null; then
  sudo apt-get update -q
  sudo apt-get install -y ydotool flameshot python3-gi gir1.2-atspi-2.0 \
    python3-pip portaudio19-dev python3-dev nodejs npm at-spi2-core
elif command -v dnf >/dev/null; then
  sudo dnf install -y ydotool flameshot python3-gobject at-spi2-core \
    python3-pip portaudio-devel python3-devel nodejs npm
else
  echo "Unsupported distro — install manually: ydotool flameshot python3-gi at-spi2 portaudio nodejs"; exit 1
fi

# 2) Python deps
python3 -m pip install --user --quiet aiohttp numpy pyaudio openwakeword onnxruntime

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
