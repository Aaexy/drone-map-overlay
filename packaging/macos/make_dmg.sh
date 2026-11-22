#!/usr/bin/env bash
# Make a drag-to-Applications disk image from the built app.
# Usage: packaging/macos/make_dmg.sh "dist/Drone Map Overlay.app" out/DroneMapOverlay-macOS.dmg
set -euo pipefail
APP="$1"
DMG="$2"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "Drone Map Overlay" -srcfolder "$STAGE" -fs HFS+ -format UDZO -imagekey zlib-level=9 -ov "$DMG" >/dev/null
echo "$DMG"
