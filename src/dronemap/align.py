"""Line the street overlay up with the picture itself.

Telemetry alone leaves the lines a few degrees off: the gimbal tilt reading is biased by
0.5-2 degrees, the compass by up to 2-3 degrees, both drifting during a flight, and the
camera's height above the ground is only known relative to an unknown take-off point. So
every couple of seconds a frame of the video is compared with satellite imagery seen
through the telemetry camera:

  * both images are reduced to edge orientation (roads, roofs and kerbs run the same way in
    a photo and in a satellite image even when their colours don't),
  * an FFT cross-correlation finds how far the picture sits from where the telemetry says
    (a small camera rotation moves the whole picture almost uniformly),
  * one height correction for the clip is chosen as the one that matches best overall.

The measured rotations are cleaned (weak or inconsistent matches dropped) and smoothed over
a few seconds, so the lines follow the picture without picking up any jitter.
NumPy only: the app doesn't ship OpenCV.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from typing import Callable, Optional

import numpy as np
from PIL import Image

from .streets import LocalFrame, Terrain
from .tiles import TILE, TileSource

log = logging.getLogger(__name__)

SAT_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
SAT_Z = 17  # ~0.8 m per pixel: finer than the comparison needs
WORK_W = 480  # frames are compared at this width
MARGIN = 64  # px at WORK_W searched around the telemetry view (~9 degrees)
RANGE_M = 1200.0  # ground further away than this is too small to compare
KEY_STEP = 2.0  # s between measured frames
SMOOTH_S = 2.5  # s, smoothing of the corrections
GAP_S = 6.0  # s; further than this from any good measurement, the clip-wide correction is used
DZ_STEPS = (-30.0, -20.0, -10.0, 0.0, 10.0)  # m, first height corrections tried
DZ_FRAMES = 16  # frames used to choose the height correction
MIN_GROUND = 0.15  # least share of the picture that must show mapped ground
MAX_TILES = 3000  # most satellite tiles kept for one clip (~200 MB)
MAX_HEIGHT_CELLS = 2_000_000  # most terrain grid cells (~8 MB)
THREADS = 4
DECODERS = 3  # video readers at once
AROUND_S = 5.0  # s either side measured for a preview
ALIGN_VERSION = 2  # 2: heading from the gimbal on steady orbits too (corrections are relative to it)

Progress = Optional[Callable[[float], None]]
Cancelled = Optional[Callable[[], bool]]


# ------------------------------------------------------------------ small image tools
def _box(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    if r < 1:
        return a
    pad = [(r + 1, r) if i == axis else (0, 0) for i in range(a.ndim)]
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float64)
    n = a.shape[axis]
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(n), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Close to a Gaussian blur: three box blurs along each axis."""
    r = max(0, int(round(math.sqrt(4 * sigma * sigma + 1) / 2 - 0.5)))
    for _ in range(3):
        a = _box(_box(a, r, 0), r, 1)
    return a


def field(gray: np.ndarray, sigma: float = 1.5) -> np.ndarray:
    """Edge orientation as a complex 'double angle' field: the same for dark-on-light and
    light-on-dark edges, magnitude compressed so a few strong edges don't dominate."""
    g = blur(gray.astype(np.float32), 1.0)
    gx = np.zeros_like(g)
    gy = np.zeros_like(g)
    gx[:, 1:-1] = g[:, 2:] - g[:, :-2]
    gy[1:-1] = g[2:] - g[:-2]
    m2 = gx * gx + gy * gy
    w = 1.0 / (np.sqrt(np.sqrt(m2)) * np.sqrt(m2) + 1e-3)
    re = blur((gx * gx - gy * gy) * w, sigma)
    im = blur(2 * gx * gy * w, sigma)
    return (re + 1j * im).astype(np.complex64)


def _fast_len(n: int) -> int:
    """Smallest 2^a 3^b 5^c >= n (fast FFT sizes)."""
    best = 1 << (n - 1).bit_length()
    f5 = 1
    while f5 < best:
        f35 = f5
        while f35 < best:
            p = f35
            while p < n:
                p *= 2
            best = min(best, p)
            f35 *= 3
        f5 *= 5
    return best


# ------------------------------------------------------------------ camera geometry
def basis(yaw: float, pitch: float) -> np.ndarray:
    """Rows: camera right, up and forward in world (east, north, up) coordinates."""
    y, p = math.radians(yaw), math.radians(pitch)
    f = np.array([math.sin(y) * math.cos(p), math.cos(y) * math.cos(p), math.sin(p)])
    r = np.array([math.cos(y), -math.sin(y), 0.0])
    return np.stack([r, np.cross(r, f), f])


def correction(pan: float, tilt: float, roll: float) -> np.ndarray:
    """Rotation in camera coordinates (degrees): turn right by pan, up by tilt, then roll
    the picture clockwise. Apply as correction(...) @ basis(...)."""
    a, b, c = math.radians(pan), math.radians(-tilt), math.radians(roll)
    ry = np.array([[math.cos(a), 0, -math.sin(a)], [0, 1, 0], [math.sin(a), 0, math.cos(a)]])
    rx = np.array([[1, 0, 0], [0, math.cos(b), math.sin(b)], [0, -math.sin(b), math.cos(b)]])
    rz = np.array([[math.cos(c), math.sin(c), 0], [-math.sin(c), math.cos(c), 0], [0, 0, 1]])
    return rz @ rx @ ry


def angles(D: np.ndarray) -> tuple[float, float, float]:
    """(pan, tilt, roll) of a camera-frame rotation made by correction()."""
    pan = math.degrees(math.atan2(D[2, 0], D[2, 2]))
    tilt = math.degrees(math.asin(max(-1.0, min(1.0, D[2, 1]))))
    roll = math.degrees(math.atan2(D[0, 1], D[1, 1]))
    return pan, tilt, roll


# ------------------------------------------------------------------ the ground
class Ground:
    """Satellite imagery and terrain around a flight, in the flight's local metric frame."""

    def __init__(self, frame: LocalFrame, terrain: Terrain, takeoff: Optional[float], offline: bool = False):
        self.frame, self.terrain, self.takeoff, self.offline = frame, terrain, takeoff, offline
        self.src = TileSource(SAT_URL, SAT_Z, mem_tiles=0)
        self.tiles: dict[tuple[int, int], np.ndarray] = {}
        self.missing: set = set()
        self._h = None  # (x0, y0, step, grid) heights relative to take-off

    def _px(self, X, Y):
        lat, lon = self.frame.latlon(X, Y)
        n = (1 << SAT_Z) * TILE
        lat = np.clip(lat, -85.0, 85.0)
        r = np.radians(lat)
        return (lon + 180.0) / 360.0 * n, (1 - np.log(np.tan(r) + 1 / np.cos(r)) / math.pi) / 2 * n

    def prefetch(self, X: np.ndarray, Y: np.ndarray, progress: Progress = None, cancelled: Cancelled = None) -> None:
        """Download the imagery and terrain covering the ground points X, Y (metres)."""
        X, Y = np.ravel(X), np.ravel(Y)
        keep = np.isfinite(X) & np.isfinite(Y)
        X, Y = X[keep], Y[keep]
        if not len(X):
            return
        px, py = self._px(X, Y)
        need = sorted({(int(a) // TILE, int(b) // TILE) for a, b in zip(px, py)})
        if len(need) > MAX_TILES:  # never fetch a whole region by mistake
            log.warning("Alignment: %d satellite tiles asked for, using the first %d", len(need), MAX_TILES)
            need = need[:MAX_TILES]
        need = [k for k in need if k not in self.tiles and k not in self.missing]
        if need and not self.offline:
            self.src.prefetch(need, progress, cancelled)
        for k in need:
            arr = None
            if os.path.exists(self.src._path(*k)):
                try:
                    with Image.open(self.src._path(*k)) as im:
                        arr = np.asarray(im.convert("L"), dtype=np.uint8)
                except Exception as e:
                    log.info("Bad satellite tile %s: %s", k, e)
            if arr is not None and arr.shape == (TILE, TILE):
                self.tiles[k] = arr
            else:
                self.missing.add(k)
        self._heights_for(X, Y)

    def _heights_for(self, X, Y, step: float = 10.0) -> None:
        x0, x1 = float(np.min(X)) - 50, float(np.max(X)) + 50
        y0, y1 = float(np.min(Y)) - 50, float(np.max(Y)) + 50
        if self._h is not None:
            hx0, hy0, s, g = self._h
            if hx0 <= x0 and hy0 <= y0 and hx0 + s * (g.shape[1] - 1) >= x1 and hy0 + s * (g.shape[0] - 1) >= y1:
                return
            x0, y0 = min(x0, hx0), min(y0, hy0)
            x1, y1 = max(x1, hx0 + s * (g.shape[1] - 1)), max(y1, hy0 + s * (g.shape[0] - 1))
        # memory stays bounded whatever the area: coarser steps for very large areas
        step = max(step, math.sqrt((x1 - x0) * (y1 - y0) / MAX_HEIGHT_CELLS))
        gx = np.arange(x0, x1 + step, step)
        gy = np.arange(y0, y1 + step, step)
        GX, GY = np.meshgrid(gx, gy)
        grid = np.zeros(GX.shape, np.float32)
        if self.takeoff is not None:
            lat, lon = self.frame.latlon(GX.ravel(), GY.ravel())
            h = self.terrain.heights(lat, lon)
            if h is not None:
                grid = (h.reshape(GX.shape) - self.takeoff).astype(np.float32)
        self._h = (x0, y0, step, grid)

    def height(self, X, Y) -> np.ndarray:
        if self._h is None:
            return np.zeros(np.shape(X))
        x0, y0, s, g = self._h
        fx = np.clip((X - x0) / s, 0, g.shape[1] - 1.001)
        fy = np.clip((Y - y0) / s, 0, g.shape[0] - 1.001)
        ix, iy = fx.astype(int), fy.astype(int)
        ux, uy = fx - ix, fy - iy
        return (
            g[iy, ix] * (1 - ux) * (1 - uy)
            + g[iy, ix + 1] * ux * (1 - uy)
            + g[iy + 1, ix] * (1 - ux) * uy
            + g[iy + 1, ix + 1] * ux * uy
        )

    def sample(self, X, Y) -> tuple[np.ndarray, np.ndarray]:
        """Grey satellite image at ground points, and where it is known."""
        px, py = self._px(X, Y)
        out = np.zeros(np.shape(X), np.float32)
        ok = np.zeros(np.shape(X), bool)
        tx, ty = (px // TILE).astype(np.int64), (py // TILE).astype(np.int64)
        keys = tx * (1 << 31) + ty
        for k in np.unique(keys):
            a = (int(k) >> 31, int(k) & ((1 << 31) - 1))
            arr = self.tiles.get(a)
            if arr is None:
                continue
            m = keys == k
            u = np.clip(px[m] - a[0] * TILE - 0.5, 0, TILE - 1.001)
            v = np.clip(py[m] - a[1] * TILE - 0.5, 0, TILE - 1.001)
            iu, iv = u.astype(int), v.astype(int)
            fu, fv = u - iu, v - iv
            out[m] = (
                arr[iv, iu] * (1 - fu) * (1 - fv)
                + arr[iv, iu + 1] * fu * (1 - fv)
                + arr[iv + 1, iu] * (1 - fu) * fv
                + arr[iv + 1, iu + 1] * fu * fv
            )
            ok[m] = True
        return out, ok


def _rays(B: np.ndarray, fx: float, w: int, h: int, step: int = 1) -> np.ndarray:
    xs, ys = np.meshgrid(np.arange(0, w, step) - w / 2 + 0.5, np.arange(0, h, step) - h / 2 + 0.5)
    return xs[..., None] / fx * B[0] - ys[..., None] / fx * B[1] + B[2]


def ground_points(ground: Optional[Ground], B, C, fx, w, h, step: int = 1, max_d: float = RANGE_M):
    """Where each pixel's ray meets the ground: X, Y and whether it does (within max_d)."""
    d = _rays(B, fx, w, h, step)
    z = np.zeros(d.shape[:2])
    X = Y = None
    ok = np.zeros(d.shape[:2], bool)
    for _ in range(3):  # terrain intersection by fixed point: plenty for gentle ground
        with np.errstate(all="ignore"):
            s = (z - C[2]) / d[..., 2]
        ok = np.isfinite(s) & (s > 0)
        X = np.where(ok, C[0] + s * d[..., 0], C[0])
        Y = np.where(ok, C[1] + s * d[..., 1], C[1])
        ok &= np.hypot(X - C[0], Y - C[1]) < max_d
        # rays that miss (sky, beyond max_d) point at the camera's own spot, never far away
        X, Y = np.where(ok, X, C[0]), np.where(ok, Y, C[1])
        if ground is None:
            break
        z = ground.height(X, Y)
    return X, Y, ok


# ------------------------------------------------------------------ one frame
class _View:
    """The satellite view around the telemetry camera, ready to correlate frames against."""

    def __init__(self, ground: Ground, B, C, fx, w, h, margin):
        self.w, self.h, self.m = w, h, margin
        W, H = w + 2 * margin, h + 2 * margin
        X, Y, ok = ground_points(ground, B, C, fx, W, H)
        img, known = ground.sample(X, Y)
        M = (ok & known).astype(np.float32)
        M = (blur(M, 3.0) > 0.99).astype(np.float32)
        R = field(img)
        self.Mt = M[margin : margin + h, margin : margin + w]
        self.ground_share = float(self.Mt.mean())
        self.shape = (_fast_len(H + h), _fast_len(W + w))
        fft = lambda a: np.fft.fft2(a, self.shape)
        FMt = np.conj(fft(self.Mt.astype(np.complex64)))
        self.FR = fft(R * M)
        n_out = (H - h + 1, W - w + 1)
        self.n_out = n_out
        eR = np.fft.ifft2(fft((np.abs(R) ** 2 * M).astype(np.complex64)) * FMt).real[: n_out[0], : n_out[1]]
        ov = np.fft.ifft2(fft(M.astype(np.complex64)) * FMt).real[: n_out[0], : n_out[1]] / max(self.Mt.sum(), 1.0)
        self.eR = np.maximum(eR, 1e-9)
        self.ov = ov

    def match(self, small: Image.Image, roll: float):
        """Offset (dx, dy) in px of the picture inside the view, score and best score elsewhere."""
        im = small.rotate(roll, resample=Image.BILINEAR) if roll else small
        T = field(np.asarray(im, np.float32))
        if roll:  # rotating leaves blank corners
            c = int(abs(math.sin(math.radians(roll))) * self.w / 2) + 4
            T[:c] = 0
            T[-c:] = 0
            T[:, :c] = 0
            T[:, -c:] = 0
        Tm = T * self.Mt
        num = np.fft.ifft2(self.FR * np.conj(np.fft.fft2(Tm, self.shape))).real[: self.n_out[0], : self.n_out[1]]
        n = num / np.sqrt(self.eR * max(float((np.abs(Tm) ** 2).sum()), 1e-9))
        n[self.ov < 0.85] = -1.0
        k = np.unravel_index(int(np.argmax(n)), n.shape)

        def sub(v, i):
            if 0 < i < len(v) - 1:
                a, b, c = v[i - 1], v[i], v[i + 1]
                den = a - 2 * b + c
                return i + (0.5 * (a - c) / den if den < 0 else 0.0)
            return float(i)

        oy, ox = sub(n[:, k[1]], k[0]), sub(n[k[0], :], k[1])
        yy, xx = np.ogrid[: n.shape[0], : n.shape[1]]
        far = np.where((yy - k[0]) ** 2 + (xx - k[1]) ** 2 > 64, n, -1.0)
        return ox - self.m, oy - self.m, float(n[k]), float(far.max())


def measure(small: Image.Image, ground: Ground, B: np.ndarray, C: np.ndarray, fx: float, refine: bool = True):
    """Rotation (camera frame) taking the telemetry camera B onto the picture `small`
    (a grey frame WORK_W wide), with the match score and the best other score.
    None if too little mapped ground is in view."""
    w, h = small.size
    v = _View(ground, B, C, fx, w, h, MARGIN)
    if v.ground_share < MIN_GROUND:
        return None
    best = max((v.match(small, r) + (r,) for r in (-1.0, 0.0, 1.0)), key=lambda m: m[2])
    dx, dy, sc, sc2, roll = best
    D = correction(math.degrees(math.atan2(dx, fx)), -math.degrees(math.atan2(dy, fx)), roll)
    if refine:  # again from there: removes the small-angle approximation, finer roll
        v = _View(ground, D @ B, C, fx, w, h, MARGIN // 3)
        if v.ground_share >= MIN_GROUND:
            best = max((v.match(small, r) + (r,) for r in (-0.5, 0.0, 0.5)), key=lambda m: m[2])
            dx, dy, sc, sc2, roll = best
            D = correction(math.degrees(math.atan2(dx, fx)), -math.degrees(math.atan2(dy, fx)), roll) @ D
    return D, sc, sc2


# ------------------------------------------------------------------ the corrections
class Alignment:
    """Smooth camera corrections over a clip: (pan, tilt, roll) in degrees at any time,
    plus one height correction (metres) for the whole clip."""

    def __init__(self, meas: np.ndarray, dz: float):
        # meas rows: t, pan, tilt, roll, weight
        self.meas, self.dz = meas, float(dz)
        good = meas[meas[:, 4] > 0] if len(meas) else meas
        self.n_good = len(good)
        if not len(good):
            self.t = np.array([0.0])
            self.c = np.zeros((1, 3))
            return
        base = np.array([_wmedian(good[:, k], good[:, 4]) for k in (1, 2, 3)])
        t0, t1 = float(meas[0, 0]), float(meas[-1, 0])
        self.t = np.arange(t0 - 1.0, t1 + 1.0 + 1e-9, 0.1)
        dt = self.t[:, None] - good[None, :, 0]
        k = np.exp(-0.5 * (dt / SMOOTH_S) ** 2) * good[None, :, 4]
        ksum = k.sum(1)
        # far from good measurements, ease toward the clip-wide correction
        near = np.clip(ksum / (ksum.max() * 0.15 + 1e-9), 0, 1)
        prior = 1e-3
        with np.errstate(all="ignore"):  # spurious floating-point flags from some BLAS builds
            smooth = (k @ good[:, 1:4] + prior * base) / (ksum + prior)[:, None]
        gap = np.min(np.abs(dt), axis=1)
        fade = np.clip((GAP_S - gap) / (GAP_S / 2), 0, 1)[:, None] * near[:, None]
        self.c = fade * smooth + (1 - fade) * base

    def at(self, t: float) -> tuple[float, float, float]:
        if len(self.t) == 1:
            return tuple(float(v) for v in self.c[0])
        return tuple(float(np.interp(t, self.t, self.c[:, k])) for k in range(3))

    def to_json(self) -> dict:
        return {"dz": self.dz, "meas": np.round(self.meas, 4).tolist()}

    @classmethod
    def from_json(cls, d: dict) -> Alignment:
        m = np.array(d["meas"], dtype=float).reshape(-1, 5)
        return cls(m, d["dz"])


def _wmedian(v: np.ndarray, w: np.ndarray) -> float:
    o = np.argsort(v)
    c = np.cumsum(w[o])
    return float(v[o][np.searchsorted(c, c[-1] / 2)])


def _clean(meas: np.ndarray) -> np.ndarray:
    """Zero the weight of measurements that disagree with their neighbours (wrong matches)."""
    m = meas.copy()
    for _ in range(2):
        good = m[:, 4] > 0
        if good.sum() < 3:
            break
        for i in np.flatnonzero(good):
            near = good & (np.abs(m[:, 0] - m[i, 0]) <= 3 * KEY_STEP + 0.01)
            near[i] = False
            if near.sum() < 2:
                continue
            ref = np.array([_wmedian(m[near, k], m[near, 4]) for k in (1, 2)])
            if np.max(np.abs(m[i, 1:3] - ref)) > 1.5:
                m[i, 4] = 0.0
    return m


def _weight(score: float, other: float) -> float:
    """How much to trust a match: a clear, strong peak."""
    ratio = score / max(other, 1e-3)
    return float(np.clip((ratio - 1.02) / 0.06, 0, 1) * np.clip((score - 0.18) / 0.1, 0, 1))


# ------------------------------------------------------------------ frames
def read_frames(video: str, times, cancelled: Cancelled = None, progress: Progress = None):
    """[(t, grey PIL image WORK_W wide, upright)] for the keyframe at or before each time.
    A few readers work on separate stretches of the clip at once."""
    times = list(times)
    n = max(1, min(DECODERS, len(times) // 8))
    chunks = [times[i * len(times) // n : (i + 1) * len(times) // n] for i in range(n)]
    done = [0]

    def tick():
        done[0] += 1
        if progress:
            progress(min(1.0, done[0] / len(times)))

    parts = _pmap(lambda ch: _read_chunk(video, ch, cancelled, tick), chunks)
    out, seen = [], set()
    for ft, img in sorted((x for part in parts for x in part), key=lambda x: x[0]):
        if ft not in seen:
            seen.add(ft)
            out.append((ft, img))
    return out


def _read_chunk(video: str, times, cancelled: Cancelled, tick) -> list:
    import av

    from .pipeline import _frame_rotation, main_video_stream

    out = []
    with av.open(video) as c:
        vs = main_video_stream(c)
        vs.thread_type = "SLICE"  # one frame at a time after each seek: no frame-thread delay
        start = vs.start_time or 0
        for t in times:
            if cancelled and cancelled():
                break
            c.seek(max(0, int(t / vs.time_base)) + start, stream=vs, backward=True)
            for fr in c.decode(vs):
                if fr.pts is None:
                    continue
                rot = _frame_rotation(fr)
                W, H = (fr.height, fr.width) if rot in (90, 270) else (fr.width, fr.height)
                h = max(2, int(round(WORK_W * H / W)))
                tw, th = (h, WORK_W) if rot in (90, 270) else (WORK_W, h)
                g = fr.reformat(width=tw, height=th, format="gray").to_ndarray()
                img = Image.fromarray(np.ascontiguousarray(g))
                if rot:
                    img = img.rotate(-rot, expand=True)
                out.append((float((fr.pts - start) * fr.time_base), img))
                break
            tick()
    return out


# ------------------------------------------------------------------ the whole clip
def align_clip(
    video: str,
    path,
    ground: Ground,
    hfov: float,
    z_offset: float = 0.0,
    progress: Optional[Callable[[str, float], None]] = None,
    cancelled: Cancelled = None,
    around: Optional[float] = None,
) -> Optional[Alignment]:
    """Measure and smooth the camera corrections for a clip. `path` is the ar.CameraPath.
    With `around` (seconds) only the few seconds around that moment are measured (a quick
    preview), unless the whole clip was done before."""
    cache = _cache_file(video, hfov, z_offset, ground.takeoff)
    if cache and os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                return Alignment.from_json(json.load(f))
        except (OSError, ValueError, KeyError):
            pass
    say = progress or (lambda stage, f: None)
    t0, t1 = float(path.t[0]), float(path.t[-1])
    times = np.arange(t0 + 0.5, t1 - 0.25, KEY_STEP)
    if around is not None:
        near = times[np.abs(times - around) <= AROUND_S]
        times = near if len(near) else times[np.argsort(np.abs(times - around))[:1]]
    # skip moments where the camera looks at the sky
    times = [float(t) for t in times if path.at(float(t)).pitch < -1.0]
    if not times:
        return None
    frames = read_frames(video, times, cancelled, lambda f: say("frames", f))
    if (cancelled and cancelled()) or not frames:
        return None
    W, H = frames[0][1].size
    cams = []
    for t, small in frames:
        p = path.at(t)
        fx = (max(W, H) / 2) / math.tan(math.radians(hfov) / 2) * p.zoom
        cams.append((t, small, basis(p.yaw, p.pitch), np.array([p.x, p.y, p.z + z_offset]), fx))
    # imagery for everything the frames may show, with the height corrections tried
    pts = []
    for _t, _s, B, C, fx in cams:
        for dz in (min(DZ_STEPS), 0.0):
            X, Y, ok = ground_points(None, B, C + [0, 0, dz], fx, W + 2 * MARGIN, H + 2 * MARGIN, step=8)
            pts.append(np.column_stack([X[ok], Y[ok]]))
    pts = np.concatenate(pts) if pts else np.zeros((0, 2))
    if not len(pts):
        return None
    ground.prefetch(pts[:, 0], pts[:, 1], lambda f: say("imagery", f), cancelled)
    if cancelled and cancelled():
        return None
    if not ground.tiles:
        log.info("Alignment: no satellite imagery")
        return None

    # 1. one height correction for the clip, from a spread of frames
    pick = cams[:: max(1, len(cams) // DZ_FRAMES)][:DZ_FRAMES]
    scores: dict[float, float] = {}

    def total(dz):
        if dz not in scores:
            rs = _pmap(lambda c: measure(c[1], ground, c[2], c[3] + [0, 0, dz], c[4], refine=False), pick)
            scores[dz] = sum(r[1] for r in rs if r)
            say("height", min(1.0, len(scores) / (len(DZ_STEPS) + 4)))
        return scores[dz]

    for dz in DZ_STEPS:
        total(dz)
        if cancelled and cancelled():
            return None
    best = max(scores, key=scores.get)
    for step in (5.0, 2.5):
        for dz in (best - step, best + step):
            total(dz)
        best = max(scores, key=scores.get)
    a, b, c = scores.get(best - 2.5), scores[best], scores.get(best + 2.5)
    dz = best
    if a is not None and c is not None and a - 2 * b + c < 0:
        dz = best + 2.5 * 0.5 * (a - c) / (a - 2 * b + c)
    log.info("Alignment: height correction %+.1f m", dz)

    # 2. the rotation at every measured frame
    rows = []
    done = [0]

    def one(c):
        if cancelled and cancelled():
            return None
        r = measure(c[1], ground, c[2], c[3] + [0, 0, dz], c[4])
        done[0] += 1
        say("align", done[0] / len(cams))
        return r

    for (t, *_rest), r in zip(cams, _pmap(one, cams)):
        if r is None:
            rows.append([t, 0.0, 0.0, 0.0, 0.0])
        else:
            D, sc, sc2 = r
            pan, tilt, roll = angles(D)
            rows.append([t, pan, tilt, roll, _weight(sc, sc2)])
    if cancelled and cancelled():
        return None
    meas = _clean(np.array(rows))
    al = Alignment(meas, dz)
    log.info("Alignment: %d of %d frames matched", al.n_good, len(rows))
    if cache and around is None:
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache + ".tmp", "w", encoding="utf-8") as f:
                json.dump(al.to_json(), f)
            os.replace(cache + ".tmp", cache)
        except OSError as e:
            log.info("Couldn't cache the alignment: %s", e)
    return al


def _pmap(fn, items):
    """map() over a few threads: the heavy lifting is NumPy and FFTs, which mostly run
    outside Python's lock."""
    from concurrent.futures import ThreadPoolExecutor

    n = min(THREADS, os.cpu_count() or 1, len(items))
    if n <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(fn, items))


def _cache_file(video: str, hfov: float, z_offset: float, takeoff: Optional[float]) -> Optional[str]:
    try:
        from .settings import cache_dir

        st = os.stat(video)
        key = "%s|%d|%d|%.2f|%.1f|%s|%d" % (
            os.path.abspath(video),
            st.st_size,
            int(st.st_mtime),
            hfov,
            z_offset,
            "%.1f" % takeoff if takeoff is not None else "-",
            ALIGN_VERSION,
        )
        return os.path.join(cache_dir(), "align", hashlib.sha1(key.encode()).hexdigest() + ".json")
    except OSError:
        return None
