#!/usr/bin/env bash
# Build a .deb (installs to /opt/drone-map-overlay, adds a menu entry and a launcher).
# Usage: packaging/linux/make_deb.sh "dist/Drone Map Overlay" 0.1.0 out/drone-map-overlay_0.1.0_amd64.deb
set -euo pipefail
SRC="$1"
VERSION="$2"
OUT="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PKG="$(mktemp -d)/pkg"
mkdir -p "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/bin" "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/256x256/apps"
cp -R "$SRC" "$PKG/opt/drone-map-overlay"
ln -s "/opt/drone-map-overlay/Drone Map Overlay" "$PKG/usr/bin/drone-map-overlay"
cp "$HERE/drone-map-overlay.desktop" "$PKG/usr/share/applications/"
python3 -c "from PIL import Image; Image.open('$ROOT/src/dronemap/assets/icon.png').resize((256, 256), Image.LANCZOS).save('$PKG/usr/share/icons/hicolor/256x256/apps/drone-map-overlay.png')"
SIZE=$(du -sk "$PKG/opt" | cut -f1)
cat > "$PKG/DEBIAN/control" <<CONTROL
Package: drone-map-overlay
Version: $VERSION
Section: video
Priority: optional
Architecture: amd64
Installed-Size: $SIZE
Depends: libegl1, libgl1, libxkbcommon0, libxkbcommon-x11-0, libfontconfig1, libdbus-1-3, libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libxcb-shape0, libxcb-randr0, libxcb-render-util0, libxcb-xinerama0
Maintainer: Drone Map Overlay
Description: Burn a live GPS map and flight stats into drone videos
 Drop in a drone video and get back a copy with a live map and flight stats.
CONTROL
dpkg-deb --root-owner-group --build "$PKG" "$OUT" >/dev/null
echo "$OUT"
