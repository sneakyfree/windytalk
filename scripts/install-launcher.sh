#!/usr/bin/env bash
# Install a GNOME app-grid entry so Windy Talk opens with a double-click (no terminal).
set -e
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
chmod +x "$REPO/desktop/launch.sh"
cat > "$APPS/windytalk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Windy Talk
Comment=Local voice control powered by the Veron 5090
Exec=bash "$REPO/desktop/launch.sh"
Icon=$REPO/desktop/build/icon.png
Terminal=false
Categories=AudioVideo;
StartupNotify=true
EOF
update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed 'Windy Talk' to the app grid."
echo "Search for it in Activities, or run: gtk-launch windytalk"
