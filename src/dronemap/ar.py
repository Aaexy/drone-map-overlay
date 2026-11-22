"""Street lines and names drawn into the picture, where the streets really are.

The camera is modelled as a pinhole at the drone's GPS position and height, looking along
the drone's heading and tilted by the gimbal. OpenStreetMap streets, raised onto the real
terrain, are projected through it for every frame.

Telemetry is noisy and updates ~10 times a second while the gimbal keeps the real picture
smooth, so the camera path is rebuilt as a smooth curve first: position and height are
smoothed more (GPS jitter), angles less (they change fast and the IMU is precise).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from typing import NamedTuple, Optional

import numpy as np
from PIL import Image, ImageDraw

from .graphics import TEXT_LOCK, font, hex_rgb
from .streets import LocalFrame, Terrain, bbox_around, fetch_streets, merge_ways
from .telemetry import Track

log = logging.getLogger(__name__)

HFOV = 68.0  # degrees across the long side at zoom 1 (DJI Mini 5 Pro, measured from video)
GRID_HZ = 50.0  # the camera path is rebuilt on a regular grid at this rate
POS_SIGMA = 0.5  # s, smoothing of position and height
ANG_SIGMA = 0.08  # s, smoothing of heading and tilt
STEP_M = 5.0  # streets are split into pieces this long (terrain, clipping)
SPACING_M = 20.0  # closest spacing between possible name spots along a street
NEAR_M = 3.0  # nothing closer than this in front of the camera is drawn
MAX_NAMES = 16  # most names on screen at once
MAX_PER_STREET = 2
RETRY_S = 60.0  # a failed street download is retried after this long (settings preview)
SS = 2  # lines are drawn at this supersampling factor, then reduced (anti-aliasing)


def has_camera_data(track: Track) -> bool:
    return sum(1 for s in track.samples if s.pitch is not None) >= max(2, len(track.samples) // 2)


# ------------------------------------------------------------------ camera path
def _knots(t: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Telemetry holds each reading until the next update, so sampled at the frame rate a
    value is a staircase. Rebuild the motion through the update moments with a smooth
    (Catmull-Rom) curve and evaluate it at `t`."""
    if len(v) < 3:
        return v.astype(float)
    ch = np.flatnonzero(np.diff(v) != 0) + 1
    idx = [0]
    for i in ch.tolist():
        while t[i] - t[idx[-1]] > 0.6:  # a long hold: keep it flat
            j = int(np.searchsorted(t, t[idx[-1]] + 0.5))
            if j >= i:
                break
            idx.append(j)
        idx.append(i)
    if idx[-1] != len(v) - 1:
        idx.append(len(v) - 1)
    kt, kv = t[idx], v[idx].astype(float)
    if len(kt) < 3:
        return np.interp(t, kt, kv)
    j = np.clip(np.searchsorted(kt, t, side="right") - 1, 0, len(kt) - 2)
    j0, j2, j3 = np.maximum(j - 1, 0), j + 1, np.minimum(j + 2, len(kt) - 1)
    t0, t1, t2, t3 = kt[j0], kt[j], kt[j2], kt[j3]
    p0, p1, p2, p3 = kv[j0], kv[j], kv[j2], kv[j3]
    h = t2 - t1
    with np.errstate(divide="ignore", invalid="ignore"):
        u = np.clip(np.where(h > 0, (t - t1) / h, 0.0), 0, 1)
        m1 = np.where(t2 != t0, (p2 - p0) / (t2 - t0) * h, 0.0)
        m2 = np.where(t3 != t1, (p3 - p1) / (t3 - t1) * h, 0.0)
    u2, u3 = u * u, u * u * u
    return (2 * u3 - 3 * u2 + 1) * p1 + (u3 - 2 * u2 + u) * m1 + (-2 * u3 + 3 * u2) * p2 + (u3 - u2) * m2


def _gauss(v: np.ndarray, sigma: float) -> np.ndarray:
    """Centred Gaussian (uses past and future: smooth without lag), edges renormalised."""
    if sigma <= 0 or len(v) < 3:
        return v
    r = int(3 * sigma) + 1
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    num = np.convolve(v, k, mode="same")
    den = np.convolve(np.ones_like(v), k, mode="same")
    return num / den


class Pose(NamedTuple):
    x: float  # metres east of the frame origin
    y: float  # metres north
    z: float  # metres above take-off
    yaw: float  # degrees, 0 = north, clockwise
    pitch: float  # degrees, negative = looking down
    zoom: float


class CameraPath:
    def __init__(self, track: Track, frame: LocalFrame):
        s = [x for x in track.samples if x.pitch is not None]
        t = np.array([x.t for x in s])
        keep = np.concatenate([[True], np.diff(t) > 1e-6])
        s = [x for x, k in zip(s, keep) if k]
        t = t[keep]
        x, y = frame.enu(np.array([q.lat for q in s]), np.array([q.lon for q in s]))
        raw = {
            "x": x,
            "y": y,
            "z": np.array([q.alt or 0.0 for q in s]),
            "yaw": np.degrees(np.unwrap(np.radians([q.yaw or 0.0 for q in s]))),
            "pitch": np.array([q.pitch for q in s]),
            "zoom": np.array([q.zoom or 1.0 for q in s]),
        }
        n = max(2, int((t[-1] - t[0]) * GRID_HZ) + 1)
        self.t = np.linspace(t[0], t[-1], n)
        dt = (self.t[-1] - self.t[0]) / max(1, n - 1) or 1.0
        self.ch = {}
        for k, v in raw.items():
            curve = np.interp(self.t, t, _knots(t, v))
            sig = (POS_SIGMA if k in ("x", "y", "z") else ANG_SIGMA if k != "zoom" else 0.1) / dt
            self.ch[k] = _gauss(curve, sig)

    def at(self, t: float) -> Pose:
        g = lambda k: float(np.interp(t, self.t, self.ch[k]))
        return Pose(g("x"), g("y"), g("z"), g("yaw") % 360.0, g("pitch"), max(1.0, g("zoom")))


class Camera:
    def __init__(self, pose: Pose, W: int, H: int, hfov: float, z_offset: float = 0.0):
        yaw, pitch = math.radians(pose.yaw), math.radians(pose.pitch)
        self.C = np.array([pose.x, pose.y, pose.z + z_offset])
        f = np.array([math.sin(yaw) * math.cos(pitch), math.cos(yaw) * math.cos(pitch), math.sin(pitch)])
        r = np.array([math.cos(yaw), -math.sin(yaw), 0.0])
        self.f, self.r, self.u = f, r, np.cross(r, f)
        self.W, self.H = W, H
        # the lens' field of view spans the long side, also for vertical videos
        self.fx = (max(W, H) / 2) / math.tan(math.radians(hfov) / 2) * pose.zoom

    def project(self, P: np.ndarray):
        """Screen x, y and depth (metres along the view) for an (N, 3) array of points."""
        v = P - self.C
        with np.errstate(all="ignore"):  # points level with the camera; spurious BLAS flags
            cz = v @ self.f
            inv = self.fx / cz
            return self.W / 2 + (v @ self.r) * inv, self.H / 2 - (v @ self.u) * inv, cz


# ------------------------------------------------------------------ geometry
class _Geometry:
    """Camera path plus streets laid on the terrain, in a local metric frame."""

    def __init__(self, track: Track, offline: bool):
        self.track, self.offline = track, offline
        s0 = track.samples[0]
        self.frame = LocalFrame(s0.lat, s0.lon)
        self.path = CameraPath(track, self.frame) if has_camera_data(track) else None
        self.terrain = Terrain(offline)
        self.lock = threading.Lock()
        self.ready = False
        self.error: Optional[str] = None
        self.takeoff: Optional[float] = None
        self.P = np.zeros((0, 3))
        self.seg = np.zeros(0, bool)
        self.major_pt = np.zeros(0, bool)
        self.anchors = None
        self.names: list[str] = []
        self.built_at = 0.0

    def prefetch(self, max_dist: float, progress=None, cancelled=None) -> None:
        with self.lock:
            self._prefetch(max_dist, progress, cancelled)

    def _prefetch(self, max_dist, progress, cancelled) -> None:
        if self.ready or self.path is None:
            return
        if self.offline:
            self._build([])
            return
        lats = [s.lat for s in self.track.samples]
        lons = [s.lon for s in self.track.samples]
        bbox = bbox_around(lats, lons, max_dist + 150)
        try:
            ways = merge_ways(fetch_streets(bbox))
        except Exception as e:
            self.error = str(e)
            log.warning("Street names: %s", e)
            ways = []
        if cancelled and cancelled():
            return
        if ways:
            # terrain under the flight and the streets
            la = np.concatenate([np.array([p[0] for w in ways for p in w["pts"]]), lats])
            lo = np.concatenate([np.array([p[1] for w in ways for p in w["pts"]]), lons])
            self.terrain.prefetch(la, lo, progress, cancelled)
        self._build(ways)

    def _build(self, ways: list[dict]) -> None:
        first = self.track.samples[0]
        h0 = self.terrain.heights([first.lat], [first.lon]) if ways else None
        # Heights are relative to the take-off point. The clip may start in the air, so the
        # best available guess for the take-off ground is the ground under the first fix.
        self.takeoff = float(h0[0]) if h0 is not None else None
        pts, line_id, names, major = [], [], [], []
        for i, w in enumerate(ways):
            x, y = self.frame.enu(np.array([p[0] for p in w["pts"]]), np.array([p[1] for p in w["pts"]]))
            seg = np.hypot(np.diff(x), np.diff(y))
            n = np.maximum(1, np.ceil(seg / STEP_M).astype(int))
            # densify: n points per segment, plus the last one
            u = np.concatenate([np.arange(k) / k for k in n.tolist()] + [np.array([0.0])])
            a = np.concatenate([np.repeat(np.arange(len(seg)), n), [len(seg) - 1]])
            u[-1] = 1.0
            dx = x[a] + (x[a + 1] - x[a]) * u
            dy = y[a] + (y[a + 1] - y[a]) * u
            pts.append(np.column_stack([dx, dy]))
            line_id.append(np.full(len(dx), i))
            names.append(w["name"])
            major.append(w["major"])
        if pts:
            P2 = np.concatenate(pts)
            lid = np.concatenate(line_id)
            z = np.zeros(len(P2))
            if self.takeoff is not None:
                lat, lon = self.frame.latlon(P2[:, 0], P2[:, 1])
                h = self.terrain.heights(lat, lon)
                if h is not None:
                    z = h - self.takeoff
            self.P = np.column_stack([P2, z])
            self.seg = lid[:-1] == lid[1:]  # segment i joins point i and i + 1
            self.major_pt = np.array(major)[lid]
            self.anchors = self._anchors(self.P, lid, len(ways))
        self.names = names
        self.ready = True
        self.built_at = time.monotonic()
        log.info(
            "Street overlay: %d streets, %d points, take-off ground %s",
            len(ways),
            len(self.P),
            "%.1f m" % self.takeoff if self.takeoff is not None else "unknown (flat ground assumed)",
        )

    @staticmethod
    def _anchors(P: np.ndarray, lid: np.ndarray, n_lines: int):
        """Fixed name spots every SPACING_M along each street, numbered k = 0, 1, 2...
        Spot k has level = how many times 2 divides k (spot 0 counts as the highest), so
        thinning to every 2nd / 4th / 8th spot keeps the same spots in place."""
        pos, dirs, ks, ids = [], [], [], []
        for i in range(n_lines):
            Q = P[lid == i]
            if len(Q) < 2:
                continue
            d = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(Q[:, :2], axis=0).T))])
            if d[-1] < SPACING_M * 0.5:
                continue
            at = np.arange(SPACING_M / 2, d[-1], SPACING_M) if d[-1] >= SPACING_M else np.array([d[-1] / 2])
            px = np.interp(at, d, Q[:, 0])
            py = np.interp(at, d, Q[:, 1])
            pz = np.interp(at, d, Q[:, 2])
            j = np.clip(np.searchsorted(d, at) - 1, 0, len(Q) - 2)
            v = Q[j + 1, :2] - Q[j, :2]
            v /= np.maximum(1e-9, np.hypot(v[:, 0], v[:, 1]))[:, None]
            pos.append(np.column_stack([px, py, pz]))
            dirs.append(v)
            ks.append(np.arange(len(at)))
            ids.append(np.full(len(at), i))
        if not pos:
            return None
        return np.concatenate(pos), np.concatenate(dirs), np.concatenate(ks), np.concatenate(ids)


_GEOM: dict = {}
_GEOM_LOCK = threading.Lock()


# ------------------------------------------------------------------ the layer
class StreetsLayer:
    """Full-frame transparent image with street lines and names for any time t. Thread-safe."""

    margin = 0

    def __init__(self, track: Track, opts: dict, style: dict, W: int, H: int, offline: bool = False):
        self.track, self.o, self.W, self.H = track, opts, int(W), int(H)
        self.offline = offline
        self.S = min(self.W, self.H) / 2160.0
        self.max_dist = float(opts.get("max_distance", 800))
        self.hfov = float(opts.get("fov", HFOV))
        self.z_offset = float(opts.get("height_offset", 0.0))
        col = opts.get("line_color", "white")
        self.line_rgb = hex_rgb(style.get("accent", "#ff5c36")) if col == "accent" else hex_rgb(_COLORS.get(col, col))
        self.text_scale = float(opts.get("text_size", 1.0))
        self.show_lines = bool(opts.get("lines", True))
        self.show_names = bool(opts.get("names", True))
        self._tags: dict = {}
        self._rot: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        # The geometry (camera path, streets on the terrain) depends only on the flight and
        # the distance limit: share it between layers (the settings preview makes many).
        self._key = (id(track), len(track.samples), track.duration, self.max_dist, bool(offline))
        with _GEOM_LOCK:
            g = _GEOM.get(self._key)
        if g is not None and g.error and time.monotonic() - g.built_at > RETRY_S:
            g = None  # the download failed a while ago: try again
        if g is None:
            g = _Geometry(track, offline)
        self.g = g

    @property
    def ready(self) -> bool:
        return self.g.ready

    @property
    def error(self) -> Optional[str]:
        return self.g.error

    @property
    def takeoff(self) -> Optional[float]:
        return self.g.takeoff

    @property
    def path(self) -> Optional[CameraPath]:
        return self.g.path

    def prefetch(self, progress=None, cancelled=None) -> None:
        """Download streets and terrain for the flight (cached), then build the geometry."""
        self.g.prefetch(self.max_dist, progress, cancelled)
        if self.g.ready:
            with _GEOM_LOCK:
                _GEOM[self._key] = self.g
                while len(_GEOM) > 3:
                    _GEOM.pop(next(iter(_GEOM)))

    # ----- drawing
    def render_parts(self, t: float):
        """(RGBA image, x, y): everything drawn at time t, cropped to where there's something
        (x, y even), or None if nothing is visible."""
        if not self.ready or self.path is None or not len(self.g.P):
            return None
        cam = Camera(self.path.at(t), self.W, self.H, self.hfov, self.z_offset)
        dist = np.hypot(self.g.P[:, 0] - cam.C[0], self.g.P[:, 1] - cam.C[1])
        sx, sy, cz = cam.project(self.g.P)
        lines = self._lines(sx, sy, cz, dist) if self.show_lines else None
        names = self._names(cam) if self.show_names and self.g.anchors is not None else []
        rects = [(x, y, x + im.width, y + im.height) for im, x, y in ([lines] if lines else []) + names]
        if not rects:
            return None
        x0 = max(0, min(r[0] for r in rects)) // 2 * 2
        y0 = max(0, min(r[1] for r in rects)) // 2 * 2
        x1 = min(self.W, max(r[2] for r in rects))
        y1 = min(self.H, max(r[3] for r in rects))
        x1, y1 = min(self.W, x1 + (x1 - x0) % 2), min(self.H, y1 + (y1 - y0) % 2)
        if x1 <= x0 or y1 <= y0:
            return None
        canvas = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        if lines:
            canvas.paste(lines[0], (lines[1] - x0, lines[2] - y0))
        for im, x, y in names:
            _composite_at(canvas, im, x - x0, y - y0)
        return canvas, x0, y0

    def render(self, t: float) -> Image.Image:
        layer = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        parts = self.render_parts(t)
        if parts:
            layer.paste(parts[0], (parts[1], parts[2]))
        return layer

    def render_frame(self, t: float, fmt: str):
        """The layer as a video frame in `fmt` (yuva420p / yuva420p10le). Only the part with
        content is converted; the rest is filled in as fully transparent."""
        import av

        frame = av.VideoFrame(self.W, self.H, fmt)
        dtype = np.uint16 if fmt.endswith("10le") else np.uint8
        planes = [_plane_array(p, dtype) for p in frame.planes]
        for a in planes:
            a[:] = 0
        parts = self.render_parts(t)
        if parts:
            img, x0, y0 = parts
            small = av.VideoFrame.from_bytes(img.tobytes(), img.width, img.height, format="rgba").reformat(format=fmt)
            for i, (dst, src) in enumerate(zip(planes[:3], (_plane_array(p, dtype) for p in small.planes))):
                sub = 2 if i in (1, 2) else 1  # 4:2:0 chroma
                h, w = -(-img.height // sub), -(-img.width // sub)
                dst[y0 // sub : y0 // sub + h, x0 // sub : x0 // sub + w] = src[:h, :w]
            # Alpha straight from the image: FFmpeg builds disagree on scaling it to 10 bits.
            a = np.asarray(img.getchannel("A"), dtype=dtype)
            if dtype is np.uint16:
                a = (a << 2) | (a >> 6)  # 0..255 -> 0..1023
            planes[3][y0 : y0 + img.height, x0 : x0 + img.width] = a
        return frame

    def _fade(self, dist):
        """1 near, fading out over the last quarter of the distance limit."""
        return np.clip((self.max_dist - dist) / (0.25 * self.max_dist), 0.0, 1.0)

    def _lines(self, sx, sy, cz, dist):
        """(RGBA image, x, y) of the street lines, or None."""
        W, H, S = self.W, self.H, self.S
        ok = (cz > NEAR_M) & (dist < self.max_dist)
        seg = self.g.seg & ok[:-1] & ok[1:]
        if not seg.any():
            return None
        # keep coordinates sane for points just in front of the camera
        lim = 4 * max(W, H)
        sx, sy = np.clip(sx, -lim, lim), np.clip(sy, -lim, lim)
        i = np.flatnonzero(seg)
        # perspective: lines get thinner with distance; drawn in a few width classes
        depth = (cz[i] + cz[i + 1]) / 2
        base = np.where(self.g.major_pt[i], 9.0, 6.5) * S * float(self.o.get("line_width", 1.0))
        width = base * np.clip(260.0 / np.maximum(depth, 1.0), 0.55, 1.25)
        wcls = np.round(width * SS).astype(int)
        fade = np.round(self._fade((dist[i] + dist[i + 1]) / 2) * 8).astype(int)
        draw_ok = fade > 0
        i, wcls, fade = i[draw_ok], wcls[draw_ok], fade[draw_ok]
        if not len(i):
            return None
        xs = np.concatenate([sx[i], sx[i + 1]])
        ys = np.concatenate([sy[i], sy[i + 1]])
        pad = int(wcls.max() / SS) + int(6 * S) + 2
        x0 = int(max(0, math.floor(xs.min()) - pad))
        y0 = int(max(0, math.floor(ys.min()) - pad))
        x1 = int(min(W, math.ceil(xs.max()) + pad))
        y1 = int(min(H, math.ceil(ys.max()) + pad))
        if x1 <= x0 or y1 <= y0:
            return None
        cw, chh = x1 - x0, y1 - y0
        core = Image.new("L", (cw * SS, chh * SS), 0)
        halo = Image.new("L", (cw * SS, chh * SS), 0)
        dc, dh = ImageDraw.Draw(core), ImageDraw.Draw(halo)
        X = (sx - x0) * SS
        Y = (sy - y0) * SS
        halo_w = int(round(4 * S * SS))
        # runs of consecutive segments with the same width and fade, far ones first
        brk = np.ones(len(i), bool)
        brk[1:] = (i[1:] != i[:-1] + 1) | (wcls[1:] != wcls[:-1]) | (fade[1:] != fade[:-1])
        starts = np.flatnonzero(brk)
        ends = np.append(starts[1:], len(i))
        runs = sorted(zip(starts.tolist(), ends.tolist()), key=lambda r: wcls[r[0]])
        for a, b in runs:
            idx = np.append(i[a:b], i[b - 1] + 1)
            xy = np.column_stack([X[idx], Y[idx]]).ravel().tolist()
            w = int(wcls[a])
            level = int(fade[a]) * 255 // 8
            # (no rounded joints: PIL draws those slowly, and at 5 m steps they're invisible)
            dh.line(xy, fill=level, width=w + 2 * halo_w)
            dc.line(xy, fill=level, width=w)
        core = core.reduce(SS)
        halo = halo.reduce(SS).point(lambda v: v * 95 // 255)
        crop = Image.new("RGBA", (cw, chh), (0, 0, 0, 0))
        crop.putalpha(halo)
        crop.paste(self.line_rgb + (235,), (0, 0, cw, chh), core)
        return crop, x0, y0

    def _tag(self, name: str, size: int, angle: float) -> tuple[Image.Image, int]:
        """(the name rotated by `angle` degrees, its unrotated width); cached in half-degree steps."""
        a2 = int(round(angle * 2))
        key = (name, size, a2)
        with self._lock:
            rot = self._rot.get(key)
            if rot is not None:
                self._rot.move_to_end(key)
                return rot
            flat = self._tags.get((name, size))
        if flat is None:
            f = font(size, 700)
            stroke = max(2, int(round(size * 0.12)))
            with TEXT_LOCK:
                tw = f.getlength(name)
                flat = Image.new("RGBA", (int(tw + 4 * stroke + 8), int(size * 1.9)), (0, 0, 0, 0))
                ImageDraw.Draw(flat).text(
                    (flat.width / 2, flat.height / 2),
                    name,
                    font=f,
                    fill=(255, 255, 255, 255),
                    anchor="mm",
                    stroke_width=stroke,
                    stroke_fill=(0, 0, 0, 200),
                )
        rot = flat.rotate(-a2 / 2.0, resample=Image.BICUBIC, expand=True) if a2 else flat
        rot = (rot, flat.width)
        with self._lock:
            if len(self._tags) > 400:
                self._tags.clear()
            self._tags[(name, size)] = flat
            self._rot[key] = rot
            while len(self._rot) > 1500:
                self._rot.popitem(last=False)
        return rot

    def _names(self, cam: Camera) -> list:
        """[(RGBA image, x, y)] for each name shown, top-left corners in frame pixels."""
        W, H, S = self.W, self.H, self.S
        A, D, K, ids = self.g.anchors
        dist = np.hypot(A[:, 0] - cam.C[0], A[:, 1] - cam.C[1])
        sx, sy, cz = cam.project(A)
        ok = (cz > 8) & (dist < self.max_dist) & (sx > -50 * S) & (sx < W + 50 * S) & (sy > -50 * S) & (sy < H + 50 * S)
        if not ok.any():
            return []
        A, D, K, ids, dist, sx, sy, cz = A[ok], D[ok], K[ok], ids[ok], dist[ok], sx[ok], sy[ok], cz[ok]
        # direction of the street on screen at each spot
        B = A.copy()
        B[:, :2] += D * 6.0
        ex, ey, ez = cam.project(B)
        good = ez > 2
        px_per_m = np.where(good, np.hypot(ex - sx, ey - sy) / 6.0, 0.0)
        ang = np.degrees(np.arctan2(ey - sy, ex - sx))
        # Ground-pinned level of detail: aim for about one name per `gap` pixels along a
        # street. Spots never move: extra ones fade out as the view zooms out.
        gap = 1100.0 * S * self.text_scale
        want_m = gap / np.maximum(px_per_m, 1e-6)
        lv = np.log2(np.maximum(1.0, want_m / SPACING_M))
        lvl = _twos(K)
        lod = np.clip((lvl + 1 - lv) / 0.35, 0.0, 1.0)
        # streets seen end-on have no room for a name
        side = np.clip((px_per_m * 6 - 8 * S) / (10 * S), 0.0, 1.0)
        alpha = lod * side * self._fade(dist)
        order = np.argsort(dist)
        placed: list[tuple[float, float, float, float]] = []
        base = 42 * S * self.text_scale
        out: list = []
        per_street: dict[str, int] = {}
        for j in order.tolist():
            a = float(alpha[j])
            if a < 0.04:
                continue
            # a little bigger close up, a little smaller far away
            size = int(round(base * float(np.clip((350.0 / max(cz[j], 1.0)) ** 0.25, 0.8, 1.2)) / 2) * 2)
            text = self.g.names[int(ids[j])]
            g = float(ang[j])
            g = g - 180 if g > 90 else (g + 180 if g < -90 else g)  # keep text upright
            if per_street.get(text, 0) >= MAX_PER_STREET:
                continue
            rot, flat_w = self._tag(text, max(10, size), g)
            rad = math.radians(g)
            # overlap test on the text's rotated footprint (its bounding box, slightly shrunk)
            hw, hh = flat_w / 2 * 0.92, size * 0.55
            ex_ = abs(hw * math.cos(rad)) + abs(hh * math.sin(rad))
            ey_ = abs(hw * math.sin(rad)) + abs(hh * math.cos(rad))
            x, y = float(sx[j]), float(sy[j])
            box = (x - ex_, y - ey_, x + ex_, y + ey_)
            if any(not (box[2] < q[0] or box[0] > q[2] or box[3] < q[1] or box[1] > q[3]) for q in placed):
                continue
            placed.append(box)
            per_street[text] = per_street.get(text, 0) + 1
            if a < 0.999:
                rot = rot.copy()
                rot.putalpha(rot.getchannel("A").point(lambda v, a=a: int(v * a)))
            out.append((rot, int(round(x - rot.width / 2)), int(round(y - rot.height / 2))))
            if len(out) >= MAX_NAMES:
                break
        return out


def _twos(k: np.ndarray) -> np.ndarray:
    """How many times 2 divides each k (k = 0 counts as dividing many times)."""
    k = k.astype(np.int64)
    out = np.full(k.shape, 30)
    nz = k != 0
    out[nz] = np.log2(k[nz] & -k[nz]).astype(int)
    return out


def _composite_at(canvas: Image.Image, im: Image.Image, x: int, y: int) -> None:
    """alpha_composite `im` with its top-left at (x, y), clipped to the canvas."""
    sx0, sy0 = max(0, -x), max(0, -y)
    dx0, dy0 = max(0, x), max(0, y)
    w = min(im.width - sx0, canvas.width - dx0)
    h = min(im.height - sy0, canvas.height - dy0)
    if w > 0 and h > 0:
        canvas.alpha_composite(im, (dx0, dy0), (sx0, sy0, sx0 + w, sy0 + h))


def _plane_array(plane, dtype) -> np.ndarray:
    """Writable 2-D view of a video frame plane (rows may be padded)."""
    a = np.frombuffer(plane, dtype=np.uint8).reshape(plane.height, plane.line_size)
    return a.view(dtype)[:, : plane.width]


_COLORS = {"white": "#ffffff", "yellow": "#ffd23e"}
