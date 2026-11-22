"""Slippy-map tiles: download once, cache on disk, stitch any region on demand."""

from __future__ import annotations

import logging
import math
import os
import ssl
import threading
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from PIL import Image, ImageDraw

from . import __version__
from .settings import cache_dir

TILE = 256
USER_AGENT = "drone-map-overlay/%s" % __version__
log = logging.getLogger(__name__)
_ssl: Optional[ssl.SSLContext] = None


def ssl_context() -> ssl.SSLContext:
    """HTTPS context using the bundled certifi CA list. Packaged apps can't rely on the
    interpreter finding the system's certificates (on macOS, every download failed)."""
    global _ssl
    if _ssl is None:
        try:
            import certifi

            _ssl = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # certifi missing: fall back to the system default
            _ssl = ssl.create_default_context()
    return _ssl


def world_px(lat: float, lon: float, z: int):
    """Web-Mercator pixel coordinates at zoom z."""
    s = TILE * (1 << z)
    lat = max(-85.0511, min(85.0511, lat))
    x = (lon + 180.0) / 360.0 * s
    r = math.radians(lat)
    y = (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * s
    return x, y


def metres_per_px(lat: float, z: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / (1 << z)


class TileSource:
    def __init__(self, url_template: str, z: int, mem_tiles: int = 256, post=None):
        self.post = post
        self.url = url_template
        self.z = z
        self.dir = os.path.join(cache_dir(), "tiles", _slug(url_template), str(z))
        self.mem: OrderedDict[tuple, Image.Image] = OrderedDict()
        self.mem_tiles = mem_tiles
        self.lock = threading.Lock()
        self.failed = 0
        self.bad: set = set()
        self.last_error: Optional[str] = None
        self._blank = Image.new("RGB", (TILE, TILE), (224, 224, 224))

    def _path(self, x, y):
        return os.path.join(self.dir, str(x), "%d.png" % y)

    def _download(self, x, y) -> bool:
        p = self._path(x, y)
        if os.path.exists(p):
            return True
        n = 1 << self.z
        if not (0 <= y < n):
            return False
        with self.lock:
            if (x, y) in self.bad:  # already failed this session: don't retry every frame
                return False
        url = self.url.format(z=self.z, x=x % n, y=y, s="a")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20, context=ssl_context()) as r:
                data = r.read()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            return True
        except Exception as e:
            with self.lock:
                self.failed += 1
                self.bad.add((x, y))
                first = self.last_error is None
                self.last_error = str(getattr(e, "reason", None) or e)
            if first:
                log.warning("Map tile download failed (%s): %s", url, self.last_error)
            return False

    def prefetch(self, tiles, progress=None, cancelled=None):
        tiles = [t for t in tiles if not os.path.exists(self._path(*t))]
        if not tiles:
            return
        done = 0
        # The OSM tile usage policy asks for at most 2 parallel downloads.
        with ThreadPoolExecutor(max_workers=2) as ex:
            for _ in ex.map(lambda t: self._download(*t), tiles):
                done += 1
                if progress:
                    progress(done / len(tiles))
                if cancelled and cancelled():
                    ex.shutdown(wait=False, cancel_futures=True)
                    return

    def tile(self, x, y) -> Image.Image:
        key = (x, y)
        with self.lock:
            im = self.mem.get(key)
            if im is not None:
                self.mem.move_to_end(key)
                return im
        p = self._path(x, y)
        if not os.path.exists(p):
            self._download(x, y)
        try:
            im = Image.open(p).convert("RGB")
        except Exception:
            im = self._blank
        if self.post:
            im = self.post(im)
        with self.lock:
            self.mem[key] = im
            while len(self.mem) > self.mem_tiles:
                self.mem.popitem(last=False)
        return im

    def region(self, x0: float, y0: float, x1: float, y1: float):
        """Stitch tiles covering world-pixel rect; returns (image, origin_x, origin_y)."""
        tx0, ty0 = int(math.floor(x0 / TILE)), int(math.floor(y0 / TILE))
        tx1, ty1 = int(math.floor(x1 / TILE)), int(math.floor(y1 / TILE))
        im = Image.new("RGB", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE))
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                im.paste(self.tile(tx, ty), ((tx - tx0) * TILE, (ty - ty0) * TILE))
        return im, tx0 * TILE, ty0 * TILE


def tiles_along(points, radius_px: float):
    """Tile indices within radius_px (world pixels) of a polyline of world-pixel points."""
    out = set()
    r = int(math.ceil(radius_px / TILE))
    last = None
    for x, y in points:
        if last and math.hypot(x - last[0], y - last[1]) < TILE / 2:
            continue
        last = (x, y)
        cx, cy = int(x // TILE), int(y // TILE)
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                out.add((cx + dx, cy + dy))
    if points:
        x, y = points[-1]
        cx, cy = int(x // TILE), int(y // TILE)
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                out.add((cx + dx, cy + dy))
    return sorted(out)


def _slug(url: str) -> str:
    keep = "".join(c if c.isalnum() else "_" for c in url.split("//")[-1].split("{")[0])
    return keep.strip("_")[:60] or "tiles"


class PlaceholderTiles(TileSource):
    """Offline stand-in map (settings preview without a video, tests), drawn in the
    colours of the chosen style so the preview still looks right."""

    PALETTES = {
        # background, park, building, road, minor road
        "light": ((245, 245, 243), (232, 236, 230), (226, 226, 224), (255, 255, 255), (250, 250, 250)),
        "dark": ((38, 38, 40), (34, 40, 36), (48, 48, 50), (70, 70, 72), (58, 58, 60)),
        "color": ((242, 239, 233), (205, 235, 176), (217, 208, 201), (255, 255, 255), (255, 255, 255)),
        "satellite": ((74, 86, 62), (58, 82, 48), (128, 120, 108), (168, 162, 150), (140, 134, 122)),
    }

    def __init__(self, z: int, style: str = "light"):
        super().__init__("placeholder-" + style, z)
        self.palette = self.PALETTES.get(style, self.PALETTES["light"])

    def prefetch(self, tiles, progress=None, cancelled=None):
        return

    def tile(self, x, y):
        key = (x, y)
        with self.lock:
            im = self.mem.get(key)
        if im is not None:
            return im
        bg, park, building, road, minor = self.palette
        im = Image.new("RGB", (TILE, TILE), bg)
        d = ImageDraw.Draw(im)
        if (x * 7 + y * 3) % 5 == 0:
            d.rectangle([40, 50, 150, 140], fill=park)
        if (x + y) % 4 == 1:
            d.rectangle([130, 20, 230, 90], fill=building)
        d.line([(0, 200), (TILE, 200)], fill=minor, width=6)
        d.line([(0, 128), (TILE, 128)], fill=road, width=14)
        d.line([(128, 0), (128, TILE)], fill=road, width=10)
        with self.lock:
            self.mem[key] = im
        return im
