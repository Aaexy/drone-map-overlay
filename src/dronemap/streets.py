"""Street and terrain data for the street-name overlay.

Streets come from OpenStreetMap (Overpass API), ground heights from the free AWS Terrain
Tiles (Terrarium encoding). Both are downloaded once per area and cached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import numpy as np
from PIL import Image

from .settings import cache_dir
from .tiles import USER_AGENT, TileSource, ssl_context

log = logging.getLogger(__name__)

R_EARTH = 6378137.0
# Public Overpass servers, tried in turn. They're free and often busy: a busy one answers
# 429/502/503/504 or a "runtime error" remark, so a failed round is retried after a pause.
OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
RETRY_WAITS = (5.0, 20.0)  # pauses before the 2nd and 3rd round over the servers
FETCH_BUDGET_S = 240.0  # give up after this long in all
_now, _sleep = time.monotonic, time.sleep  # (tests replace these)
# Streets people know by name. Footpaths, cycleways, driveways and parking aisles are left
# out: they'd bury the map in labels.
ROAD_TYPES = (
    "motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|pedestrian|road"
    "|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"
)
MAJOR = {"motorway", "trunk", "primary", "secondary", "motorway_link", "trunk_link", "primary_link"}
DATA_VERSION = 1
TERRAIN_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TERRAIN_Z = 14  # ~6-10 m per pixel: plenty for where roads are


class DataError(RuntimeError):
    pass


class LocalFrame:
    """Flat-earth metres east/north around a fixed origin (fine over a few km)."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = math.radians(1) * R_EARTH * math.cos(math.radians(lat0))
        self.ky = math.radians(1) * R_EARTH

    def enu(self, lat, lon):
        return (np.asarray(lon) - self.lon0) * self.kx, (np.asarray(lat) - self.lat0) * self.ky

    def latlon(self, x, y):
        return self.lat0 + np.asarray(y) / self.ky, self.lon0 + np.asarray(x) / self.kx


# ------------------------------------------------------------------ OpenStreetMap
def bbox_around(lats, lons, margin_m: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) around the points plus `margin_m`, snapped outwards to a
    0.005 degree grid so nearby flights share the cache."""
    lat_c = (min(lats) + max(lats)) / 2
    dlat = margin_m / 111320.0
    dlon = margin_m / (111320.0 * max(0.1, math.cos(math.radians(lat_c))))
    g = 0.005
    return (
        math.floor((min(lats) - dlat) / g) * g,
        math.floor((min(lons) - dlon) / g) * g,
        math.ceil((max(lats) + dlat) / g) * g,
        math.ceil((max(lons) + dlon) / g) * g,
    )


def _post(url: str, data: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
            return r.read()
    except urllib.error.URLError as e:
        # Some older Python builds can't negotiate TLS with every server; curl can.
        if "SSL" not in str(e.reason) and "TLS" not in str(e.reason):
            raise
        log.info("TLS with %s failed in Python (%s); trying curl", url, e.reason)
        try:
            return subprocess.run(
                ["curl", "-fsS", "--max-time", str(int(timeout)), "-A", USER_AGENT, "--data-binary", "@-", url],
                input=data,
                capture_output=True,
                check=True,
                timeout=timeout + 5,
            ).stdout
        except (OSError, subprocess.SubprocessError) as e2:
            raise e from e2


def _busy(e: Exception) -> bool:
    """True if the server is up but couldn't answer now (worth asking again later)."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code == 429 or e.code >= 500
    if isinstance(e, urllib.error.URLError):
        e = e.reason if isinstance(e.reason, Exception) else e
    return isinstance(e, (socket.timeout, TimeoutError, ConnectionResetError, ValueError, _Busy))


class _Busy(Exception):
    pass


def _describe(e: Exception) -> str:
    return str(getattr(e, "reason", None) or e)


def _download(body: bytes, timeout: float, cancelled=None) -> Optional[dict]:
    """The Overpass answer from the first server that gives a complete one. Rounds over the
    servers are repeated after a pause while any of them is only busy; None if cancelled."""
    deadline = _now() + FETCH_BUDGET_S
    err: Optional[Exception] = None
    busy = False
    for rnd in range(len(RETRY_WAITS) + 1):
        if rnd:
            if not busy or _now() + RETRY_WAITS[rnd - 1] + 10 > deadline:
                break  # nobody's busy (no network?) or no time left: waiting won't help
            log.info("Street servers busy; trying again in %.0f s", RETRY_WAITS[rnd - 1])
            end = _now() + RETRY_WAITS[rnd - 1]
            while _now() < end:
                if cancelled and cancelled():
                    return None
                _sleep(0.25)
        busy = False
        for url in OVERPASS:
            if cancelled and cancelled():
                return None
            left = deadline - _now()
            if left < 10:
                break
            try:
                data = json.loads(_post(url, body, min(timeout, left)))
                remark = str(data.get("remark", ""))
                if "error" in remark.lower():  # e.g. timed out: the elements are incomplete
                    raise _Busy(remark)
                return data
            except Exception as e:  # try the next server
                err, busy = e, busy or _busy(e)
                log.warning("Street download from %s failed: %s", url, _describe(e))
    if busy:
        raise DataError(
            "Street names couldn't be downloaded: OpenStreetMap's servers are busy (%s). "
            "Try again in a few minutes." % _describe(err)
        )
    raise DataError("Street names couldn't be downloaded (%s)." % _describe(err))


def fetch_streets(bbox: tuple[float, float, float, float], timeout: float = 90, cancelled=None) -> list[dict]:
    """Named streets in `bbox` as [{"name", "highway", "pts": [(lat, lon), ...]}], cached.
    [] if `cancelled()` turns true while waiting for a busy server."""
    key = "%.3f,%.3f,%.3f,%.3f|%d" % (*bbox, DATA_VERSION)
    path = os.path.join(cache_dir(), "streets", hashlib.sha1(key.encode()).hexdigest() + ".json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    q = '[out:json][timeout:%d];way["highway"~"^(%s)$"]["name"](%f,%f,%f,%f);out geom;' % (
        int(timeout),
        ROAD_TYPES,
        *bbox,
    )
    data = _download(urllib.parse.urlencode({"data": q}).encode(), timeout, cancelled)
    if data is None:  # cancelled
        return []
    ways = [
        {
            "name": e["tags"]["name"],
            "highway": e["tags"].get("highway", ""),
            "pts": [(g["lat"], g["lon"]) for g in e["geometry"]],
        }
        for e in data.get("elements", [])
        if e.get("type") == "way" and "geometry" in e and len(e["geometry"]) > 1
    ]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ways, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as e:
        log.info("Couldn't cache streets: %s", e)
    log.info("Streets: %d named ways in %s", len(ways), bbox)
    return ways


def merge_ways(ways: list[dict]) -> list[dict]:
    """Join pieces of the same street that meet end to end. OpenStreetMap splits streets
    wherever a property changes, and labels would otherwise bunch up at every split."""
    key = lambda p: (round(p[0], 7), round(p[1], 7))
    by_name: dict[str, list[dict]] = {}
    for w in ways:
        by_name.setdefault(w["name"], []).append({"highway": w["highway"], "pts": list(map(tuple, w["pts"]))})
    out = []
    for name, parts in by_name.items():
        parts = [p for p in parts if len(p["pts"]) > 1]
        while parts:
            cur = parts.pop()
            pts, hw = cur["pts"], {cur["highway"]}
            grown = True
            while grown:
                grown = False
                for i, p in enumerate(parts):
                    q = p["pts"]
                    if key(q[0]) == key(pts[-1]):
                        pts = pts + q[1:]
                    elif key(q[-1]) == key(pts[-1]):
                        pts = pts + q[::-1][1:]
                    elif key(q[-1]) == key(pts[0]):
                        pts = q[:-1] + pts
                    elif key(q[0]) == key(pts[0]):
                        pts = q[::-1][:-1] + pts
                    else:
                        continue
                    hw.add(p["highway"])
                    parts.pop(i)
                    grown = True
                    break
            out.append({"name": name, "major": bool(hw & MAJOR), "pts": pts})
    return out


# ------------------------------------------------------------------ terrain
class Terrain:
    """Ground height above sea level (m) from Terrarium elevation tiles."""

    def __init__(self, offline: bool = False):
        self.src = TileSource(TERRAIN_URL, TERRAIN_Z, mem_tiles=64)
        self.offline = offline
        self._arrays: dict[tuple[int, int], Optional[np.ndarray]] = {}

    def _xy(self, lat, lon):
        n = 1 << TERRAIN_Z
        lat = np.clip(np.asarray(lat, dtype=float), -85.0, 85.0)
        r = np.radians(lat)
        fx = (np.asarray(lon, dtype=float) + 180.0) / 360.0 * n * 256
        fy = (1 - np.log(np.tan(r) + 1 / np.cos(r)) / math.pi) / 2 * n * 256
        return fx, fy

    def prefetch(self, lats, lons, progress=None, cancelled=None) -> None:
        if self.offline:
            return
        fx, fy = self._xy(lats, lons)
        tiles = sorted({(int(a) // 256, int(b) // 256) for a, b in zip(np.ravel(fx), np.ravel(fy))})
        self.src.prefetch(tiles, progress, cancelled)

    def _tile(self, tx: int, ty: int) -> Optional[np.ndarray]:
        if (tx, ty) not in self._arrays:
            arr = None
            if not self.offline and self.src._download(tx, ty):
                try:
                    rgb = np.asarray(Image.open(self.src._path(tx, ty)).convert("RGB"), dtype=np.float64)
                    arr = rgb[..., 0] * 256 + rgb[..., 1] + rgb[..., 2] / 256 - 32768
                except Exception as e:
                    log.info("Bad terrain tile %s: %s", (tx, ty), e)
            self._arrays[(tx, ty)] = arr
        return self._arrays[(tx, ty)]

    def heights(self, lat, lon) -> Optional[np.ndarray]:
        """Bilinear ground heights for arrays of positions; None if any tile is missing."""
        fx, fy = self._xy(lat, lon)
        fx, fy = np.ravel(fx), np.ravel(fy)
        out = np.empty(fx.shape)
        tx, ty = (fx // 256).astype(int), (fy // 256).astype(int)
        for key in set(zip(tx.tolist(), ty.tolist())):
            arr = self._tile(*key)
            if arr is None:
                return None
            m = (tx == key[0]) & (ty == key[1])
            px = np.clip(fx[m] - key[0] * 256 - 0.5, 0, 254.999)
            py = np.clip(fy[m] - key[1] * 256 - 0.5, 0, 254.999)
            x0, y0 = px.astype(int), py.astype(int)
            ux, uy = px - x0, py - y0
            out[m] = (
                arr[y0, x0] * (1 - ux) * (1 - uy)
                + arr[y0, x0 + 1] * ux * (1 - uy)
                + arr[y0 + 1, x0] * (1 - ux) * uy
                + arr[y0 + 1, x0 + 1] * ux * uy
            )
        return out
