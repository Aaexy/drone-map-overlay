#!/usr/bin/env bash
# Build a single-file AppImage from the PyInstaller folder.
# Usage: packaging/linux/make_appimage.sh "dist/Drone Map Overlay" out/DroneMapOverlay-x86_64.AppImage
set -euo pipefail
SRC="$1"
OUT="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
APPDIR="$(mktemp -d)/DroneMapOverlay.AppDir"
mkdir -p "$APPDIR/usr/lib"
cp -R "$SRC" "$APPDIR/usr/lib/drone-map-overlay"
cp "$HERE/drone-map-overlay.desktop" "$APPDIR/"
cp "$ROOT/src/dronemap/assets/icon.png" "$APPDIR/drone-map-overlay.png"
cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/drone-map-overlay/Drone Map Overlay" "$@"
RUN
chmod +x "$APPDIR/AppRun"
TOOL="${APPIMAGETOOL:-appimagetool}"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream "$APPDIR" "$OUT"
echo "$OUT"
