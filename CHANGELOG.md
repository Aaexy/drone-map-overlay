# Changelog

## 0.2.0

### New

- **Street names:** street lines and names drawn into the picture where the streets really
  are, pinned to the ground as the drone moves, turns and tilts. Uses the camera angle and
  zoom from DJI's telemetry, OpenStreetMap streets and terrain heights (downloaded once per
  area and cached). Shown live in the settings preview; options for lines, names, colour,
  thickness, text size, distance and a height fine-tune. Off by default.
- **Presets:** save the look and layout under a name, switch between them from the settings
  window, import and export them as files. Built-in starting points: Default, Minimal,
  Satellite explorer, Night and Street explorer.
- Command line: `--preset NAME`, `--list-presets`, `--streets` / `--no-streets`.

### Fixed

- Clips that end with the drone landed (or hovering, or facing exactly north) lost their
  altitude, speed or heading for those moments: DJI's telemetry leaves out values that are
  zero.

## 0.1.0

First release.

### Features

- Drop in a drone video and get back a copy with a live map and flight stats burned in.
- GPS from DJI's built-in telemetry (no extra files needed; tested on the Mini 5 Pro) or a
  DJI `.SRT` caption file.
- Map styles: Light, Dark (grayscale), Color and Satellite. On first launch the map matches
  the computer's light or dark mode.
- Map options: rounded or circular, north-up or direction-of-flight-up, whole flight / recent
  / no trail, arrow or dot marker, camera direction, route ahead, north marker, border,
  opacity, map credit.
- Flight stats: speed, altitude, vertical speed, heading, distance flown, distance from start,
  time and coordinates; side by side or stacked; dark or light panel; metric or imperial.
- Layout editor with a live preview of your own video: drag and resize the map and stats,
  scrub through time, reset positions, right-click to reset or hide one box. Layouts adapt to
  landscape, vertical and square videos.
- Settings window with Save & render; dark, light or system theme.
- Render queue: drop several videos or a folder; progress, cancel, retry, show in folder.
- Command line: `dronemap VIDEO`, with `--preview`, `--settings` and `--limit`.

### Quality and speed

- Keeps 10-bit HEVC and the original bitrate (or H.264 for compatibility), audio, colour
  information and recording date; rotated videos come out upright.
- Hardware encoding on macOS; overlay drawn on several cores ahead of the encoder.
- Reads GPS straight from the video's index and caches it; renders to the internal disk and
  copies to a memory card while rendering. A 6-minute 4K/60 clip renders to an SD card in
  about 6 minutes on an M4 Pro.

### Packages

- Windows: installer (per-user, no admin needed) and portable zip.
- macOS: disk image and zip.
- Linux: AppImage, `.deb` and portable tar.gz.
- Portable versions keep their settings and map cache next to the app.

### License

- Released under the GNU General Public License v3 or later. Third-party licenses are listed
  in `THIRD_PARTY_LICENSES.md` and included with every package.

### Known limitations

- The apps aren't code-signed yet, so macOS and Windows show a warning the first time.
- On Windows and Linux, encoding runs on the CPU.
