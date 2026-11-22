"""Slippy-map tiles: download once, cache on disk, stitch any region on demand."""
from __future__ import annotations

import math
import os
import threading
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from . import __version__
from .settings import cache_dir

TILE = 256
USER_AGENT = "drone-map-overlay/%s" % __version__


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
        self.mem: "OrderedDict[tuple, Image.Image]" = OrderedDict()
        self.mem_tiles = mem_tiles
        self.lock = threading.Lock()
        self.failed = 0
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
        url = self.url.format(z=self.z, x=x % n, y=y, s="a")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            return True
        except Exception:
            with self.lock:
                self.failed += 1
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
    """Offline stand-in used by the settings preview before any video is opened."""

    def __init__(self, z: int, post=None):
        super().__init__("placeholder", z, post=post)

    def prefetch(self, tiles, progress=None, cancelled=None):
        return

    def tile(self, x, y):
        key = (x, y)
        im = self.mem.get(key)
        if im is None:
            from PIL import ImageDraw
            im = Image.new("RGB", (TILE, TILE), (232, 229, 222))
            d = ImageDraw.Draw(im)
            if (x * 7 + y * 3) % 5 == 0:
                d.rectangle([40, 50, 150, 140], fill=(205, 230, 190))
            if (x + y) % 4 == 1:
                d.rectangle([130, 20, 230, 90], fill=(216, 208, 200))
            d.line([(0, 128), (TILE, 128)], fill=(255, 255, 255), width=14)
            d.line([(128, 0), (128, TILE)], fill=(255, 255, 255), width=10)
            if self.post:
                im = self.post(im)
            self.mem[key] = im
        return im
