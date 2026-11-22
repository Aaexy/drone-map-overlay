"""Read GPS / flight telemetry from drone videos.

Sources, in order of preference:
  1. DJI's embedded "djmd" protobuf track inside the MP4 (Mini 4/5 Pro, Air 3, ...).
     No sidecar file needed.
  2. A DJI .SRT caption file next to the video (enable "Video Subtitles" in DJI Fly).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import struct
from bisect import bisect_right
from dataclasses import dataclass
from typing import Optional

import av

log = logging.getLogger(__name__)


@dataclass
class Sample:
    t: float  # seconds from start of video
    lat: float
    lon: float
    alt: Optional[float] = None  # metres above take-off
    speed: Optional[float] = None  # horizontal speed, m/s
    yaw: Optional[float] = None  # heading in degrees, 0 = north, clockwise
    vs: Optional[float] = None  # vertical speed, m/s, positive = climbing


MAX_SPEED = 120.0  # m/s; anything faster between two fixes is a GPS glitch, not flight


def drop_glitches(samples: list[Sample]) -> list[Sample]:
    """Remove isolated GPS fixes that would need an impossible speed to reach.
    Compared against the last *kept* sample, so a single bad fix can't poison the rest."""
    if len(samples) < 3:
        return samples
    kept = [samples[0]]
    for s in samples[1:]:
        prev = kept[-1]
        dt = max(s.t - prev.t, 1e-3)
        if haversine(prev.lat, prev.lon, s.lat, s.lon) / dt <= MAX_SPEED + 30.0 / dt:
            kept.append(s)
    return kept if len(kept) >= max(2, len(samples) // 2) else samples


class Track:
    """A time-ordered GPS track, with heading/speed filled in where the source lacks them."""

    def __init__(self, samples: list[Sample], source: str):
        if not samples:
            raise ValueError("empty track")
        self.raw = [Sample(s.t, s.lat, s.lon, s.alt, s.speed, s.yaw) for s in samples]
        self.samples = drop_glitches(sorted(samples, key=lambda s: s.t))
        self.source = source
        self._fill_missing()
        self._times = [x.t for x in self.samples]
        self.dist = [0.0]
        for a, b in zip(self.samples, self.samples[1:]):
            self.dist.append(self.dist[-1] + haversine(a.lat, a.lon, b.lat, b.lon))

    def _fill_missing(self):
        s = self.samples
        # Vertical speed from altitude over a ~1 s window (smooths barometer noise).
        if any(x.alt is not None for x in s):
            for i, x in enumerate(s):
                j0, j1 = _window(s, i, 0.5)
                a, b = s[j0], s[j1]
                if a.alt is not None and b.alt is not None and b.t > a.t:
                    x.vs = (b.alt - a.alt) / (b.t - a.t)
        # Derive speed / heading from positions when the source does not provide them.
        if any(x.speed is None for x in s) or any(x.yaw is None for x in s):
            for i, x in enumerate(s):
                j0, j1 = _window(s, i, 1.0)
                a, b = s[j0], s[j1]
                dt = b.t - a.t
                d = haversine(a.lat, a.lon, b.lat, b.lon)
                if x.speed is None:
                    x.speed = d / dt if dt > 0 else 0.0
                if x.yaw is None and d > 0.5:
                    x.yaw = bearing(a.lat, a.lon, b.lat, b.lon)
            last = None
            for x in s:  # carry heading through hovers
                if x.yaw is None:
                    x.yaw = last
                last = x.yaw
            first = next((x.yaw for x in s if x.yaw is not None), 0.0)
            for x in s:
                if x.yaw is None:
                    x.yaw = first

    def index_at(self, t: float) -> int:
        """Index of the last real sample at or before t (0 if t is before the track)."""
        return max(0, bisect_right(self._times, t) - 1)

    @property
    def duration(self) -> float:
        return self.samples[-1].t

    def at(self, t: float):
        """Interpolated Sample at time t, plus the index of the last real sample <= t
        and the distance flown so far."""
        s = self.samples
        if t <= s[0].t:
            return s[0], 0, 0.0
        if t >= s[-1].t:
            return s[-1], len(s) - 1, self.dist[-1]
        lo, hi = 0, len(s) - 1
        while hi - lo > 1:
            m = (lo + hi) // 2
            if s[m].t <= t:
                lo = m
            else:
                hi = m
        a, b = s[lo], s[hi]
        u = (t - a.t) / (b.t - a.t) if b.t > a.t else 0.0

        def lerp(p, q):
            if p is None or q is None:
                return p if q is None else q
            return p + (q - p) * u

        yaw = None
        if a.yaw is not None and b.yaw is not None:
            yaw = (a.yaw + ((b.yaw - a.yaw + 180) % 360 - 180) * u) % 360
        out = Sample(
            t,
            lerp(a.lat, b.lat),
            lerp(a.lon, b.lon),
            lerp(a.alt, b.alt),
            lerp(a.speed, b.speed),
            yaw if yaw is not None else a.yaw,
            lerp(a.vs, b.vs),
        )
        return out, lo, lerp(self.dist[lo], self.dist[hi])


def _window(s, i, half):
    j0 = i
    while j0 > 0 and s[i].t - s[j0 - 1].t <= half:
        j0 -= 1
    j1 = i
    while j1 < len(s) - 1 and s[j1 + 1].t - s[i].t <= half:
        j1 += 1
    if j0 == j1:
        j0, j1 = max(0, i - 1), min(len(s) - 1, i + 1)
    return j0, j1


def haversine(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


# --------------------------------------------------------------------- protobuf
def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def pb_parse(b: bytes):
    """Schema-less protobuf decode into [(field, value)], recursing into anything
    that parses as a nested message."""
    i, out = 0, []
    n = len(b)
    while i < n:
        k, i = _varint(b, i)
        f, w = k >> 3, k & 7
        if f == 0:
            raise ValueError("field 0")
        if w == 0:
            v, i = _varint(b, i)
        elif w == 1:
            if i + 8 > n:
                raise ValueError("short")
            v = struct.unpack_from("<d", b, i)[0]
            i += 8
        elif w == 5:
            if i + 4 > n:
                raise ValueError("short")
            v = struct.unpack_from("<f", b, i)[0]
            i += 4
        elif w == 2:
            ln, i = _varint(b, i)
            if i + ln > n:
                raise ValueError("short")
            v = b[i : i + ln]
            i += ln
            if v:
                try:
                    v = pb_parse(v)
                except (ValueError, IndexError, struct.error):
                    pass
        else:
            raise ValueError("wire type %d" % w)
        out.append((f, v))
    return out


def pb_shallow(b: bytes):
    """One level of protobuf: [(field, value)], length-delimited values left as bytes."""
    i, out, n = 0, [], len(b)
    while i < n:
        k, i = _varint(b, i)
        f, w = k >> 3, k & 7
        if f == 0:
            raise ValueError("field 0")
        if w == 0:
            v, i = _varint(b, i)
        elif w == 1:
            v = struct.unpack_from("<d", b, i)[0]
            i += 8
        elif w == 5:
            v = struct.unpack_from("<f", b, i)[0]
            i += 4
        elif w == 2:
            ln, i = _varint(b, i)
            v = b[i : i + ln]
            i += ln
        else:
            raise ValueError("wire type %d" % w)
        if i > n:
            raise ValueError("short")
        out.append((f, v))
    return out


def pb_path(b, path):
    """Follow `path` through nested messages, decoding only the levels on the way."""
    for depth, want in enumerate(path):
        if not isinstance(b, (bytes, bytearray)):
            return None
        for f, v in pb_shallow(b):
            if f == want:
                b = v
                break
        else:
            return None
        if depth == len(path) - 1:
            return b
    return b


def pb_get(t, path):
    for f, v in t or []:
        if f == path[0]:
            if len(path) == 1:
                return v
            return pb_get(v, path[1:]) if isinstance(v, list) else None
    return None


def _s64(x):
    return x - (1 << 64) if isinstance(x, int) and x >= 1 << 63 else x


def _find_latlon(t, depth=0):
    """Fallback for unknown models: first sub-message holding two doubles that look
    like a latitude/longitude pair."""
    if depth > 7 or not isinstance(t, list):
        return None
    doubles = [v for f, v in t if isinstance(v, float)]
    for a, b in zip(doubles, doubles[1:]):
        if -90 <= a <= 90 and -180 <= b <= 180 and (abs(a) > 1e-6 or abs(b) > 1e-6) and a != int(a) and b != int(b):
            return a, b
    for _f, v in t:
        if isinstance(v, list):
            r = _find_latlon(v, depth + 1)
            if r:
                return r
    return None


def _decode_djmd_frame(msg):
    """Pull the useful bits out of one djmd packet. Field layout verified on the
    DJI Mini 5 Pro (dvtm_Mini5Pro.proto); falls back to a heuristic for others."""
    drone = pb_get(msg, [3, 3])
    lat = pb_get(drone, [4, 1, 2])
    lon = pb_get(drone, [4, 1, 3])
    if not isinstance(lat, float) or not isinstance(lon, float):
        found = _find_latlon(msg)
        if not found:
            return None
        lat, lon = found
        return Sample(0.0, lat, lon)
    if lat == 0.0 and lon == 0.0:
        return None
    # Protobuf leaves out fields that are zero (landed: altitude 0, hovering: velocity 0,
    # facing north: yaw 0), so in this known layout a missing value means 0.
    vx, vy = pb_get(drone, [2, 1]), pb_get(drone, [2, 2])
    return _known_sample(lat, lon, vx, vy, pb_get(drone, [3, 3]), pb_get(drone, [5, 1]))


def _known_sample(lat, lon, vx, vy, yaw, alt) -> Sample:
    vx = vx if isinstance(vx, float) else 0.0
    vy = vy if isinstance(vy, float) else 0.0
    yaw = (_s64(yaw) / 10.0) % 360 if isinstance(yaw, int) else 0.0
    alt = _s64(alt) / 1000.0 if isinstance(alt, int) else (alt / 1000.0 if isinstance(alt, float) else 0.0)
    return Sample(0.0, lat, lon, alt, math.hypot(vx, vy), yaw)


def _is_djmd(stream):
    tag = getattr(stream, "codec_tag", "") or ""
    handler = stream.metadata.get("handler_name", "")
    return tag == "djmd" or handler.strip() == "CAM meta"


def _decode_djmd_fast(raw: bytes) -> Optional[Sample]:
    """Known layout (DJI Mini 5 Pro), decoding only the fields we use. Falls back to the
    full decode + heuristic search for unknown layouts."""
    try:
        drone = pb_path(raw, [3, 3])
        gps = pb_path(drone, [4, 1]) if drone is not None else None
        lat, lon = (pb_path(gps, [2]), pb_path(gps, [3])) if gps is not None else (None, None)
        if not isinstance(lat, float) or not isinstance(lon, float):
            return _decode_djmd_frame(pb_parse(raw))
        if lat == 0.0 and lon == 0.0:
            return None
        vel = pb_path(drone, [2])
        vx, vy = (pb_path(vel, [1]), pb_path(vel, [2])) if vel is not None else (None, None)
        att = pb_path(drone, [3])
        yaw = pb_path(att, [3]) if att is not None else None
        return _known_sample(lat, lon, vx, vy, yaw, pb_path(drone, [5, 1]))
    except (ValueError, IndexError, struct.error):
        return None


def read_embedded(path: str) -> Optional[Track]:
    """DJI telemetry from the MP4 itself. Reads only the telemetry samples via the MP4
    index when possible; falls back to demuxing with FFmpeg."""
    from . import mp4index

    idx = [t for t in mp4index.tracks(path) if t.format == b"djmd" or t.name == "CAM meta"]
    if idx and idx[0].samples:
        samples = []
        for t, raw in mp4index.read_samples(path, idx[0]):
            s = _decode_djmd_fast(raw)
            if s:
                s.t = t
                samples.append(s)
        if samples:
            return Track(samples, "embedded DJI telemetry")
    return _read_embedded_demux(path)


def _read_embedded_demux(path: str) -> Optional[Track]:
    with av.open(path) as c:
        streams = [s for s in c.streams if s.type == "data" and _is_djmd(s)]
        if not streams:
            return None
        st = streams[0]
        samples = []
        for p in c.demux(st):
            if not p.size or p.pts is None:
                continue
            try:
                s = _decode_djmd_fast(bytes(p))
            except (ValueError, IndexError, struct.error):
                continue
            if s:
                s.t = float(p.pts * p.time_base)
                samples.append(s)
    return Track(samples, "embedded DJI telemetry") if samples else None


# --------------------------------------------------------------------- SRT
_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->")
_KV = re.compile(r"\[?(latitude|longitude|rel_alt|abs_alt|altitude)\s*[:=]\s*(-?\d+(?:\.\d+)?)", re.I)
_GPS_OLD = re.compile(r"GPS\s*\(\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*,\s*(-?\d+(?:\.\d+)?)", re.I)
_H_OLD = re.compile(r"\bH\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*m", re.I)


def find_srt(video_path: str) -> Optional[str]:
    base = os.path.splitext(video_path)[0]
    for ext in (".SRT", ".srt"):
        if os.path.exists(base + ext):
            return base + ext
    return None


def read_srt(path: str) -> Optional[Track]:
    with open(path, encoding="utf-8", errors="replace") as f:
        blocks = re.split(r"\n\s*\n", f.read().replace("\r", ""))
    samples = []
    for b in blocks:
        m = _TS.search(b)
        if not m:
            continue
        h, mi, s, ms = m.groups()
        t = int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / (10 ** len(ms))
        kv = {k.lower(): float(v) for k, v in _KV.findall(b)}
        lat, lon, alt = kv.get("latitude"), kv.get("longitude"), kv.get("rel_alt")
        if lat is None:
            g = _GPS_OLD.search(b)
            if g:  # older DJI format: GPS(lon, lat, alt)
                lon, lat = float(g.group(1)), float(g.group(2))
                h_m = _H_OLD.search(b)
                alt = float(h_m.group(1)) if h_m else None
        if lat is None or lon is None or (lat == 0 and lon == 0):
            continue
        samples.append(Sample(t, lat, lon, alt))
    return Track(samples, "SRT captions") if samples else None


CACHE_VERSION = 2  # 2: missing (zero) protobuf fields read as 0


def _cache_file(video_path: str) -> Optional[str]:
    """Cache entry for this exact file (path, size and modification time)."""
    try:
        from .settings import cache_dir

        st = os.stat(video_path)
        key = "%s|%d|%d|%d" % (os.path.abspath(video_path), st.st_size, int(st.st_mtime), CACHE_VERSION)
        return os.path.join(cache_dir(), "telemetry", hashlib.sha1(key.encode()).hexdigest() + ".json")
    except OSError:
        return None


def _cache_load(path: Optional[str]) -> Optional[Track]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        samples = [Sample(*row) for row in d["samples"]]
        return Track(samples, d["source"]) if samples else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _cache_save(path: Optional[str], track: Track, raw: list) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = [[s.t, s.lat, s.lon, s.alt, s.speed, s.yaw] for s in raw]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"source": track.source, "samples": rows}, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as e:
        log.info("Couldn't cache telemetry: %s", e)


def load_track(video_path: str) -> Track:
    """GPS track for a video: embedded DJI telemetry first, then a sidecar .SRT.
    Embedded telemetry is cached, so opening the same clip again is instant."""
    if not os.path.exists(video_path):
        raise FileNotFoundError(video_path)
    cache = _cache_file(video_path)
    cached = _cache_load(cache)
    if cached is not None:
        return cached
    t = None
    try:
        t = read_embedded(video_path)
    except av.error.InvalidDataError:
        raise  # not a readable video at all: say that, not "no GPS"
    except av.FFmpegError as e:
        log.info("No embedded telemetry readable in %s: %s", video_path, e)
    if t is None:
        srt = find_srt(video_path)
        if srt:
            t = read_srt(srt)
    if t is None:
        from .errors import NoTelemetryError

        raise NoTelemetryError()
    if t.source == "embedded DJI telemetry":
        _cache_save(cache, t, t.raw)
    return t
