# Drone Map Overlay

Drop in a drone video, get back a copy with a **live map** and **flight stats**
(speed, altitude, heading, distance, coordinates) burned in. Works on Windows,
macOS and Linux.

![Main window](docs/main-window.png)

![Settings: drag and resize the map and stats on a live preview](docs/settings.png)

## Features

- **No extra files needed for recent DJI drones.** GPS is read from the telemetry DJI
  embeds inside the MP4 (tested on the Mini 5 Pro). Older DJI drones work too if you
  recorded with **Video Subtitles / Captions** turned on (the `.SRT` file next to the video).
- **Live mini-map** that follows the drone, in four styles: **Light** and **Dark**
  (grayscale), **Color** (the standard map) and **Satellite**. On first launch the map
  matches your computer: white in light mode, black in dark mode.
- **Make it yours:** rounded or circular map, north-up or direction-of-flight-up, whole
  flight / recent / no trail, arrow or dot marker, camera direction, route ahead, north
  marker, border, opacity, drop shadow and accent colour.
- **Flight stats:** speed, altitude, vertical speed, heading, distance flown, distance from
  the start, time and coordinates, side by side (wrapping into a grid when crowded) or
  stacked, on a dark or light panel, in metric or imperial units.
- **Layout editor:** drag and resize the map and stats directly on a frame of your own
  video and scrub through time. **Reset positions** puts everything back; right-click a
  box to reset or hide just that one. Each box is pinned to a corner, edge
  or the centre, so one layout works for landscape, vertical (9:16) and square videos at
  any resolution.
- **Keeps your quality:** 10-bit HEVC in, 10-bit HEVC out at the original bitrate
  (or H.264 for maximum compatibility). Audio, colour information and the recording date
  are kept; rotated phone-style videos come out upright.
- **Robust:** handles dropped frames and GPS glitches, never overwrites your original, only
  writes the finished file once it's complete, and checks free disk space first.
- Drop several videos (or a whole folder) to queue them.

## Download

Builds for each platform are produced by GitHub Actions on every push
(**Actions → latest run → Artifacts**) and attached to tagged releases.

- **macOS:** the app isn't signed yet. The first time, right-click the app → **Open** → **Open**.
- **Windows:** SmartScreen may warn about an unknown publisher → **More info** → **Run anyway**.
- **Linux:** extract the `.tar.gz` and run `Drone Map Overlay/Drone Map Overlay`.

## How to use

1. Open the app and drop a video onto the window (or click **Choose video…**).
2. The finished copy is saved next to the original as `NAME_map.mp4`
   (change the folder and file name ending in **Settings → Output**).
3. Click **Settings** to change what's shown and to move or resize the map and stats.

### Command line

```bash
dronemap VIDEO.MP4                 # render with your saved settings
dronemap *.MP4                     # several at once
dronemap VIDEO.MP4 --preview 12    # PNG of the frame at 12 s, to check the look
dronemap VIDEO.MP4 --settings my-layout.json -o out.mp4
```

In the packaged app, use `"Drone Map Overlay" --cli ...` with the same options.

## Speed and hardware

- **macOS:** encoding uses Apple's hardware video encoder (VideoToolbox), tuned for
  throughput. On an M4 Pro, 4K/60 10-bit footage renders faster than real time
  (a 16 s clip takes about 12–16 s).
- **Windows and Linux:** encoding runs on the CPU (x265 / x264), so 4K takes longer.
- The map and stats are drawn on several CPU cores in parallel, a few frames ahead of the
  encoder, and cost about 2 ms per frame at 4K.

## Troubleshooting

If a video can't be processed, the app says why in plain language. For anything else,
click **Log** in the app (or open the file below) and include it when reporting a problem:

| | Log file |
| --- | --- |
| macOS | `~/Library/Logs/DroneMapOverlay/dronemap.log` |
| Windows | `%LOCALAPPDATA%\DroneMapOverlay\logs\dronemap.log` |
| Linux | `~/.local/state/DroneMapOverlay/dronemap.log` |

Settings live in `settings.json` in `~/Library/Application Support/DroneMapOverlay` (macOS),
`%APPDATA%\DroneMapOverlay` (Windows) or `~/.config/DroneMapOverlay` (Linux).

## Map data and privacy

| Style | Source | Credit |
| --- | --- | --- |
| Light, Dark, Color | [OpenStreetMap](https://www.openstreetmap.org/copyright) (Light and Dark are made from it on your computer) | © OpenStreetMap contributors |
| Satellite | [Esri World Imagery](https://www.arcgis.com/home/item.html?id=10df2279f9684e4a9f6a7f08febac2a9) | Imagery © Esri, Maxar, Earthstar Geographics |

Tiles are downloaded once for the area you flew and cached on your computer, so the tile
server sees which area you requested. Both sources ask for credit when you publish: turn on
**Map credit** in Settings, or put the credit line in your video description. Esri's imagery
is provided under [Esri's terms of use](https://www.esri.com/en-us/legal/terms/full-master-agreement);
check them if you use satellite view commercially.

Each style's tile server can be changed in the settings file (`tiles.light.url`,
`tiles.satellite.url`, …), for example to use your own server or a provider you have an
API key for. Please respect the [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/)
for heavy use.

## Building from source

Python 3.9+:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                      # tests (offline)
.venv/bin/drone-map-overlay                     # run the app
.venv/bin/pyinstaller packaging/dronemap.spec   # build the app into dist/
```

Project layout:

| Path | What it does |
| --- | --- |
| `src/dronemap/telemetry.py` | Reads GPS from DJI's embedded `djmd` protobuf track or an `.SRT` file |
| `src/dronemap/tiles.py` | Downloads, caches and stitches map tiles |
| `src/dronemap/widgets.py` | Draws the map and stats widgets (thread-safe) |
| `src/dronemap/layout.py`, `overlay.py` | Where widgets go on frames of any shape |
| `src/dronemap/pipeline.py`, `encoders.py` | Decode → overlay → encode (PyAV / FFmpeg), multi-threaded |
| `src/dronemap/settings.py` | Settings, defaults, migration and where they're stored |
| `src/dronemap/errors.py`, `logs.py` | Plain-language errors and the log file |
| `src/dronemap/app/` | The desktop app (PySide6) |
| `packaging/` | App build spec, icon generator, test-video generator |

Checks run on every push: `ruff check`, `ruff format --check` and the test suite on
Windows, macOS and Linux, then each platform's app is built and test-renders a video.

### Adding another drone

`telemetry._decode_djmd_frame` knows the field layout used by the Mini 5 Pro and falls back
to searching each packet for a latitude/longitude pair. If your drone's video isn't
recognised, open an issue with a short sample clip.

## License

Not chosen yet.
