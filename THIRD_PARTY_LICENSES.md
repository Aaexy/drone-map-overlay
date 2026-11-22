# Third-party software

Drone Map Overlay is free software under the GNU General Public License, version 3 or (at
your option) any later version: see `LICENSE`. Its source code is available at
https://github.com/Aaexy/drone-map-overlay.

The packaged apps (installers, disk images, AppImage, portable versions) also contain the
following software, each under its own license. The license texts are in the `licenses`
folder that ships with the app.

| Component | License | Source |
| --- | --- | --- |
| [Python](https://www.python.org/) | PSF License | https://github.com/python/cpython |
| [Qt](https://www.qt.io/) and [PySide6](https://doc.qt.io/qtforpython/) | LGPL-3.0 | https://code.qt.io/ |
| [PyAV](https://pyav.basswood-io.com/) | BSD-3-Clause | https://github.com/PyAV-Org/PyAV |
| [FFmpeg](https://ffmpeg.org/) (bundled with PyAV), built with x264, x265 and other libraries | GPL-2.0-or-later / LGPL-2.1-or-later, depending on the library | https://ffmpeg.org/download.html, https://code.videolan.org/videolan/x264, https://bitbucket.org/multicoreware/x265_git |
| [Pillow](https://python-pillow.org/) | MIT-CMU (HPND) | https://github.com/python-pillow/Pillow |
| [certifi](https://github.com/certifi/python-certifi) | MPL-2.0 | https://github.com/certifi/python-certifi |
| [NumPy](https://numpy.org/) (with OpenBLAS on Windows and Linux) | BSD-3-Clause | https://github.com/numpy/numpy |

Qt is used as shared libraries that you can replace, as the LGPL allows.

## Map data

Map tiles are downloaded while the app runs; they are not part of the app.

- Light, Dark and Color maps: © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, available under the Open Database License.
- Satellite imagery: © Esri, Maxar, Earthstar Geographics, under
  [Esri's terms of use](https://www.esri.com/en-us/legal/terms/full-master-agreement).
- Street names: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors,
  available under the Open Database License, downloaded through the
  [Overpass API](https://overpass-api.de/).
- Ground heights: [Terrain Tiles](https://registry.opendata.aws/terrain-tiles/) on AWS Open
  Data (made from SRTM, GMTED, NED, ETOPO1 and other public sources; see their
  [attribution list](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)).
