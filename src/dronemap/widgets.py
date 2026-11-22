"""The two on-screen widgets: the live map and the flight-stats panel.

Both are safe to render from several threads at once: per-frame work only touches
local images, and the shared caches are guarded by locks.
"""

from __future__ import annotations

import math
import threading
from bisect import bisect_right
from collections.abc import Sequence
from typing import Optional

from PIL import Image, ImageDraw

from . import units
from .graphics import (
    TEXT_LOCK,
    downsample,
    ellipse_mask,
    ellipse_ring,
    font,
    hex_rgb,
    rounded_mask,
    rounded_ring,
    shadow_canvas,
    tile_style,
)
from .telemetry import Sample, Track, haversine
from .tiles import PlaceholderTiles, TileSource, tiles_along, world_px

Point = tuple[float, float]
PANEL_RGB = (14, 17, 22)


def _simplify(points: Sequence[Point], min_step: float) -> tuple[list[int], list[Point]]:
    """Drop points closer than `min_step` to the previous kept point. Returns the kept
    points and their indices in the original list, so "path up to sample i" is a
    bisect away."""
    if not points:
        return [], []
    idx, out = [0], [points[0]]
    m2 = min_step * min_step
    for i in range(1, len(points)):
        x, y = points[i]
        lx, ly = out[-1]
        if (x - lx) ** 2 + (y - ly) ** 2 >= m2:
            idx.append(i)
            out.append((x, y))
    return idx, out


def _rdp(points: Sequence[Point], eps: float) -> list[int]:
    """Douglas-Peucker: indices of the points needed to keep the line within `eps` of the
    original. Straight stretches collapse to their ends; curves keep their detail."""
    n = len(points)
    if n < 3:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    e2 = eps * eps
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        worst, wi = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            if L2 == 0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                c = (px - ax) * dy - (py - ay) * dx
                d2 = c * c / L2
            if d2 > worst:
                worst, wi = d2, i
        if worst > e2:
            keep[wi] = True
            stack.append((a, wi))
            stack.append((wi, b))
    return [i for i in range(n) if keep[i]]


def _simplify_path(points: Sequence[Point], min_step: float, eps: float) -> tuple[list[int], list[Point]]:
    """Min-distance thinning, then Douglas-Peucker; returns kept points + original indices."""
    idx, pts = _simplify(points, min_step)
    keep = _rdp(pts, eps)
    return [idx[i] for i in keep], [pts[i] for i in keep]


def _chunks(pts: Sequence[Point], size: int = 48) -> list[tuple[int, int, float, float, float, float]]:
    """(first, last, minx, miny, maxx, maxy) for overlapping runs of the path, so a frame
    only transforms the parts of a long flight that are near the view."""
    out = []
    i = 0
    while i < len(pts) - 1:
        j = min(len(pts) - 1, i + size)
        xs = [p[0] for p in pts[i : j + 1]]
        ys = [p[1] for p in pts[i : j + 1]]
        out.append((i, j, min(xs), min(ys), max(xs), max(ys)))
        i = j
    return out


def _visible_runs(pts: Sequence[Point], w: float, h: float, pad: float) -> list[list[Point]]:
    """Split a screen-space polyline into runs that touch the visible area, so long
    flights don't cost thousands of off-screen line joints per frame."""
    runs: list[list[Point]] = []
    cur: list[Point] = []
    x0, y0, x1, y1 = -pad, -pad, w + pad, h + pad
    prev_in = False
    prev: Optional[Point] = None
    for p in pts:
        inside = x0 <= p[0] <= x1 and y0 <= p[1] <= y1
        if inside or prev_in:
            if not cur and prev is not None:
                cur.append(prev)
            cur.append(p)
        elif cur:
            runs.append(cur)
            cur = []
        prev_in, prev = inside, p
    if cur:
        runs.append(cur)
    return runs


class MapWidget:
    """North-up or heading-up mini-map centred on the drone."""

    SS = 2  # supersampling for lines and markers
    PATH_STEP = 1.5  # screen px between simplified path points

    def __init__(
        self,
        track: Track,
        cfg: dict,
        style: dict,
        tiles_cfg: dict,
        w: int,
        h: int,
        px_scale: float,
        offline: bool = False,
    ):
        self.track, self.cfg = track, cfg
        self.w, self.h = max(16, int(w)), max(16, int(h))
        self.heading_up = cfg.get("orientation") == "heading"
        self.trail = cfg.get("trail", "full")
        self.trail_seconds = float(cfg.get("trail_seconds", 30))
        self.show_route = bool(cfg.get("show_full_route", False))
        self.show_cone = bool(cfg.get("show_cone", True))
        self.show_north = bool(cfg.get("show_north", True))
        self.marker = cfg.get("marker", "arrow")
        self.circle = cfg.get("shape") == "circle"
        self.accent = hex_rgb(style.get("accent", "#ff5c36"))

        # Pick the tile zoom whose pixels land closest to 1 screen px: same ground area
        # as the chosen zoom, but sharp at 8K and without shimmering at 1080p.
        z, k = int(cfg.get("zoom", 17)), float(px_scale)
        while k < 0.71 and z > 3:
            z, k = z - 1, k * 2
        while k > 1.42 and z < 19:
            z, k = z + 1, k / 2
        self.z, self.k = z, k
        map_style = cfg.get("style", "light")
        # Semi-transparent guides (camera cone, route ahead): dark on the white map, white elsewhere.
        self.ink = (0, 0, 0) if map_style == "light" else (255, 255, 255)
        source = tiles_cfg.get(map_style) or tiles_cfg["light"]
        if offline or tiles_cfg.get("offline"):
            self.tiles: TileSource = PlaceholderTiles(z, map_style)
        else:
            self.tiles = TileSource(source["url"], z, post=tile_style(map_style))

        self.world = [world_px(s.lat, s.lon, z) for s in track.samples]
        # Path within a third of a screen pixel of the GPS track, in as few points as possible.
        self.path_idx, self.path_pts = _simplify_path(self.world, self.PATH_STEP / k, 0.33 / k)
        self.path_chunks = _chunks(self.path_pts)
        self.route_pts = _simplify_path(self.world, 3 * self.PATH_STEP / k, 0.5 / k)[1]
        self.route_chunks = _chunks(self.route_pts)

        m = min(self.w, self.h)
        self.S = m / 620.0  # design units: the look was drawn for a 620 px map
        self.radius = int(m * float(style.get("roundness", 0.06)))
        self.margin = int(max(4, m * 0.045))
        opacity = max(0.2, min(1.0, float(cfg.get("opacity", 1.0))))
        bw = max(1.0, 5 * self.S)
        if self.circle:
            mask, ring = ellipse_mask(self.w, self.h), ellipse_ring(self.w, self.h, bw)
        else:
            mask, ring = rounded_mask(self.w, self.h, self.radius), rounded_ring(self.w, self.h, self.radius, bw)
        self.alpha = mask if opacity >= 1.0 else mask.point(lambda a: int(a * opacity))
        self.border = None
        if cfg.get("show_border", True):
            self.border = Image.new("RGBA", (self.w, self.h), (*PANEL_RGB, 0))
            self.border.putalpha(ring.point(lambda a: a * 200 // 255))
        self.shadow = shadow_canvas(mask, self.margin, int(110 * opacity) if style.get("shadow", True) else 0)

        self.badge_r = max(8, int(26 * self.S))
        nr, gap = self.badge_r, int(18 * self.S)
        if self.circle:  # tuck the badge inside the circle, at "half past one"
            d = (m / 2 - nr - gap) / math.sqrt(2)
            self.badge_xy = (int(self.w / 2 + d - nr), int(self.h / 2 - d - nr))
        else:
            self.badge_xy = (self.w - 2 * nr - gap, gap)
        self.north = self._north_badge(None) if self.show_north and not self.heading_up else None
        self.credit = self._credit_layer(source.get("credit", "")) if cfg.get("show_credit") else None

        self._lock = threading.Lock()
        self._region: Optional[tuple[Image.Image, int, int]] = None

    # ------------------------------------------------------------ tiles
    def view_radius_px(self) -> float:
        """Radius (tile px) of the map area needed around the drone, rotation included."""
        return math.hypot(self.w, self.h) / self.k / 2 + 4

    def prefetch(self, progress=None, cancelled=None) -> None:
        self.tiles.prefetch(tiles_along(self.world, self.view_radius_px()), progress, cancelled)

    def _base(self, cx: float, cy: float) -> tuple[Image.Image, int, int]:
        """Stitched tiles covering the view. Reused until the drone leaves it."""
        r = self.view_radius_px()
        with self._lock:
            reg = self._region
            if reg is not None:
                img, ox, oy = reg
                if ox <= cx - r and oy <= cy - r and ox + img.width >= cx + r and oy + img.height >= cy + r:
                    return reg
            pad = r + 256
            self._region = self.tiles.region(cx - pad, cy - pad, cx + pad, cy + pad)
            return self._region

    # ------------------------------------------------------------ render
    def render(self, t: float) -> Image.Image:
        cur, idx, _ = self.track.at(t)
        w, h, k, S, SS = self.w, self.h, self.k, self.S, self.SS
        yaw = cur.yaw or 0.0
        cx, cy = world_px(cur.lat, cur.lon, self.z)

        # 1) Map imagery in one resample: sub-pixel shift, scale and (heading-up) rotation.
        rot = math.radians(yaw) if self.heading_up else 0.0
        ca, sa = math.cos(rot), math.sin(rot)
        base, ox, oy = self._base(cx, cy)
        if self.heading_up:
            a, b, d, e = ca / k, -sa / k, sa / k, ca / k
            c = (cx - ox) - (w / 2) * a - (h / 2) * b
            f = (cy - oy) - (w / 2) * d - (h / 2) * e
            img = base.transform((w, h), Image.AFFINE, (a, b, c, d, e, f), resample=Image.BICUBIC)
        else:  # no rotation: a separable resize with a fractional crop box is ~4x cheaper
            x0, y0 = cx - ox - w / (2 * k), cy - oy - h / (2 * k)
            img = base.resize((w, h), Image.BICUBIC, box=(x0, y0, x0 + w / k, y0 + h / k))
        img = img.convert("RGBA")

        # 2) Vector layer at SS x, box-filtered down (premultiplied: no dark fringes).
        layer = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        dr = ImageDraw.Draw(layer)
        kk = k * SS
        hx, hy = w * SS / 2, h * SS / 2

        def P(p: Point) -> Point:
            dx, dy = (p[0] - cx) * kk, (p[1] - cy) * kk
            return hx + dx * ca + dy * sa, hy - dx * sa + dy * ca

        screen_yaw = 0.0 if self.heading_up else yaw
        if self.show_cone:
            cone = 210 * S * SS
            dr.pieslice(
                [hx - cone, hy - cone, hx + cone, hy + cone],
                screen_yaw - 128,
                screen_yaw - 52,
                fill=(*self.ink, 80 if self.ink[0] else 34),
            )
        pad = 40 * S * SS
        if self.show_route and len(self.route_pts) > 1:
            n_all = len(self.route_pts)
            for run in self._near_view(self.route_pts, self.route_chunks, n_all, P, w * SS, h * SS, pad, (cx, cy)):
                dr.line(run, fill=(*self.ink, 150 if self.ink[0] else 90), width=max(2, int(6 * S * SS)), joint="curve")
        halo = 46 * S * SS
        dr.ellipse([hx - halo, hy - halo, hx + halo, hy + halo], fill=(*self.accent, 70))
        if self.trail != "off":
            self._draw_trail(dr, P, t, idx, (hx, hy), w * SS, h * SS, pad, (cx, cy))
        self._draw_marker(dr, hx, hy, screen_yaw)
        img.alpha_composite(downsample(layer, SS))

        # 3) Decorations, shape, border, shadow.
        if self.show_north:
            img.alpha_composite(self.north if self.north is not None else self._north_badge(yaw), self.badge_xy)
        if self.credit is not None:
            img.alpha_composite(self.credit)
        img.putalpha(self.alpha)
        if self.border is not None:
            img.alpha_composite(self.border)
        out = self.shadow.copy()
        out.alpha_composite(img, (self.margin, self.margin))
        return out

    def _draw_trail(self, dr, P, t: float, idx: int, head: Point, w: float, h: float, pad: float, center: Point) -> None:
        S, SS = self.S, self.SS
        n = bisect_right(self.path_idx, idx)
        if self.trail == "recent":
            t0 = t - self.trail_seconds
            n0 = bisect_right(self.path_idx, self.track.index_at(t0))
            start, _, _ = self.track.at(t0)
            pts = [P(world_px(start.lat, start.lon, self.z))] + [P(p) for p in self.path_pts[n0:n]] + [head]
        else:
            runs = self._near_view(self.path_pts, self.path_chunks, n, P, w, h, pad, center)
            last = self.path_pts[n - 1] if n else None
            if last is not None:
                if runs and runs[-1][-1] == P(last):
                    runs[-1].append(head)
                else:
                    runs.append([P(last), head])
            self._stroke(dr, runs)
            sx, sy = P(self.world[0])
            r = 11 * S * SS
            dr.ellipse(
                [sx - r, sy - r, sx + r, sy + r], fill=(255, 255, 255), outline=(30, 30, 30), width=max(1, int(3 * S * SS))
            )
            return
        self._stroke(dr, _visible_runs(pts, w, h, pad))

    def _near_view(self, pts, chunks, n: int, P, w: float, h: float, pad: float, center: Point) -> list[list[Point]]:
        """Screen-space runs of pts[:n], transforming only chunks near the view."""
        r = (math.hypot(w, h) / 2 + pad) / (self.k * self.SS)
        cx, cy = center
        runs: list[list[Point]] = []
        prev_end = -1
        for i0, i1, x0, y0, x1, y1 in chunks:
            if i0 >= n - 1:
                break
            if x1 < cx - r or x0 > cx + r or y1 < cy - r or y0 > cy + r:
                continue
            end = min(i1, n - 1)
            seg = [P(p) for p in pts[i0 : end + 1]]
            if runs and i0 == prev_end:
                runs[-1].extend(seg[1:])
            else:
                runs.append(seg)
            prev_end = end
        return runs

    def _stroke(self, dr, runs) -> None:
        S, SS = self.S, self.SS
        for run in runs:
            if len(run) > 1:
                dr.line(run, fill=(255, 255, 255, 255), width=max(3, int(15 * S * SS)), joint="curve")
                dr.line(run, fill=(*self.accent, 255), width=max(2, int(9 * S * SS)), joint="curve")

    def _draw_marker(self, dr, hx: float, hy: float, screen_yaw: float) -> None:
        S, SS = self.S, self.SS
        if self.marker == "dot":
            r = 17 * S * SS
            dr.ellipse([hx - r, hy - r, hx + r, hy + r], fill=(255, 255, 255))
            r2 = 11 * S * SS
            dr.ellipse([hx - r2, hy - r2, hx + r2, hy + r2], fill=self.accent)
            return
        ya = math.radians(screen_yaw)
        cy, sy = math.cos(ya), math.sin(ya)

        def R(x: float, y: float) -> Point:
            return hx + x * cy - y * sy, hy + x * sy + y * cy

        L = 30 * S * SS
        dr.polygon([R(0, -L), R(0.72 * L, 0.78 * L), R(0, 0.38 * L), R(-0.72 * L, 0.78 * L)], fill=(255, 255, 255))
        dr.polygon([R(0, -0.62 * L), R(0.42 * L, 0.5 * L), R(0, 0.22 * L), R(-0.42 * L, 0.5 * L)], fill=self.accent)

    def _north_badge(self, yaw: Optional[float]) -> Image.Image:
        """N badge; in heading-up mode a needle points to true north."""
        nr = self.badge_r
        D = 2 * nr * 4
        big = Image.new("RGBA", (D, D), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        c, R = D / 2, nr * 4
        d.ellipse([0, 0, D - 1, D - 1], fill=(*PANEL_RGB, 215))
        if yaw is None:
            with TEXT_LOCK:
                d.text((c, c + 4), "N", font=font(R * 1.15, 800), fill=(255, 255, 255), anchor="mm")
        else:
            na = math.radians(-yaw)

            def pt(a: float, r: float) -> Point:
                return c + math.sin(a) * r, c - math.cos(a) * r

            d.polygon([pt(na, R * 0.82), pt(na + 2.55, R * 0.5), pt(na - 2.55, R * 0.5)], fill=self.accent)
            with TEXT_LOCK:
                d.text((c, c), "N", font=font(R * 0.72, 800), fill=(255, 255, 255), anchor="mm")
        return downsample(big, 4)

    def _credit_layer(self, credit: str) -> Optional[Image.Image]:
        if not credit:
            return None
        layer = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        S = self.S
        size = max(9.0, 15 * S)
        if self.circle:
            # Sit where the circle is wide enough, and shrink until the line fits the chord.
            y = self.h * 0.85
            r = min(self.w, self.h) / 2
            room = 2 * math.sqrt(max(0.0, r * r - (y - self.h / 2) ** 2)) * 0.86
            xy, anchor = (self.w / 2, y), "mm"
        else:
            room = self.w * 0.9
            xy, anchor = (self.w - int(14 * S), self.h - int(10 * S)), "rd"
        with TEXT_LOCK:
            while size > 8 and font(size, 500).getlength(credit) > room:
                size *= 0.92
            ImageDraw.Draw(layer).text(
                xy,
                credit,
                font=font(size, 500),
                fill=(40, 40, 40),
                anchor=anchor,
                stroke_width=max(1, int(2 * S)),
                stroke_fill=(255, 255, 255),
            )
        return layer


class StatsWidget:
    """Panel with flight numbers, side by side ("row") or stacked ("column")."""

    KEYS = ("speed", "altitude", "vspeed", "heading", "distance", "home", "time")
    LABELS = {
        "speed": "SPEED",
        "altitude": "ALTITUDE",
        "vspeed": "VERT. SPEED",
        "heading": "HEADING",
        "distance": "DISTANCE",
        "home": "FROM START",
        "time": "TIME",
    }
    # Widest value each field must fit, so the font size never jumps mid-video.
    TEMPLATES = {
        "metric": {
            "speed": ("188", "km/h"),
            "altitude": ("888", "m"),
            "vspeed": ("+88.8", "m/s"),
            "heading": ("888", "°"),
            "distance": ("8.88", "km"),
            "home": ("8.88", "km"),
            "time": ("88:88", ""),
        },
        "imperial": {
            "speed": ("188", "mph"),
            "altitude": ("1888", "ft"),
            "vspeed": ("+88.8", "mph"),
            "heading": ("888", "°"),
            "distance": ("8.88", "mi"),
            "home": ("8.88", "mi"),
            "time": ("88:88", ""),
        },
    }
    THEMES = {
        # panel, label, value, unit
        "dark": (PANEL_RGB, (150, 158, 170), (255, 255, 255), (200, 205, 212)),
        "light": ((247, 248, 250), (104, 112, 124), (16, 20, 26), (70, 76, 86)),
    }
    COORDS_TEMPLATE = "88.88888° N   888.88888° W"

    def __init__(self, track: Track, cfg: dict, style: dict, w: int, h: int):
        self.track = track
        self.w, self.h = max(16, int(w)), max(16, int(h))
        self.units = "imperial" if style.get("units") == "imperial" else "metric"
        self.keys = [k for k in self.KEYS if cfg.get(k)]
        self.coords = bool(cfg.get("coords"))
        self.column = cfg.get("layout") == "column"
        self.colors = self.THEMES["light" if cfg.get("theme") == "light" else "dark"]
        self.radius = int(min(self.w, self.h) * float(style.get("roundness", 0.06)) * 1.6)
        self.margin = int(max(4, min(self.w, self.h * 3) * 0.02))
        self.mask = rounded_mask(self.w, self.h, self.radius)
        alpha = int(255 * max(0.0, min(1.0, float(style.get("panel_opacity", 0.8)))))
        self.panel = Image.new("RGBA", (self.w, self.h), (*self.colors[0], 0))
        self.panel.putalpha(self.mask.point(lambda a: a * alpha // 255))
        strength = 90 * alpha // 255 if style.get("shadow", True) else 0
        self.shadow = shadow_canvas(self.mask, self.margin, strength)
        self.home = track.samples[0]
        with TEXT_LOCK:
            if self.column:
                self._layout_column()
            else:
                self._layout_row()
        self._lock = threading.Lock()
        self._cache_key: Optional[tuple] = None
        self._cache: Optional[Image.Image] = None

    @classmethod
    def has_content(cls, cfg: dict) -> bool:
        return any(cfg.get(k) for k in cls.KEYS) or bool(cfg.get("coords"))

    # ------------------------------------------------------------ layout
    def _field_width(self, key: str, fv, fl) -> float:
        val, unit = self.TEMPLATES[self.units][key]
        return fv.getlength(val) + (self.val_px * 0.1 + fl.getlength(unit) if unit else 0)

    def _fit_row(self, cols: int) -> tuple[float, float, float, float]:
        """Font sizes (label, value, coords, gap) for a grid with `cols` columns."""
        w, h, pad = self.w, self.h, self.pad
        n_rows = math.ceil(len(self.keys) / cols) if self.keys else 0
        colw = (w - 2 * pad) / max(1, cols)
        weights = [0.24, 0.62] * n_rows + ([0.2] if self.coords else [])
        unit = (h - 2 * pad) / (sum(weights) + 0.08 * max(0, len(weights) - 1)) if weights else 1.0
        lab, val, crd, gap = 0.24 * unit, 0.62 * unit, 0.2 * unit, 0.08 * unit
        while self.keys and val > 6:
            fv, fl = font(val, 700), font(lab, 600)
            self.val_px = val
            if max(self._field_width(k, fv, fl) for k in self.keys) <= colw * 0.94:
                break
            val *= 0.92
        return min(lab, val * 0.42), val, crd, gap

    def _layout_row(self) -> None:
        w, h = self.w, self.h
        self.pad = pad = min(w, h) * 0.16
        n = len(self.keys)
        # Try every column count; keep the one that gives the largest numbers.
        best = None
        for cols in range(max(1, n), 0, -1):
            sizes = self._fit_row(cols)
            if best is None or sizes[1] > best[1][1] * 1.02:
                best = (cols, sizes)
        self.cols, (self.lab_px, self.val_px, self.crd_px, self.gap) = best
        self.n_rows = math.ceil(n / self.cols) if n else 0
        self.colw = (w - 2 * pad) / max(1, self.cols)
        # Coordinates never outgrow the numbers, and must fit the width.
        self.crd_px = min(self.crd_px, self.val_px * 0.55) if n else self.crd_px
        while self.coords and self.crd_px > 6 and font(self.crd_px, 500).getlength(self.COORDS_TEMPLATE) > w - 2 * pad:
            self.crd_px *= 0.92
        self.row_step = self.lab_px + self.gap + self.val_px + self.gap
        block = (
            self.n_rows * self.row_step - (self.gap if n else 0) + ((self.gap if n else 0) + self.crd_px if self.coords else 0)
        )
        self.top = pad + max(0.0, (h - 2 * pad - block) / 2)

    def _layout_column(self) -> None:
        w, h = self.w, self.h
        pad = min(w, h) * 0.12
        self.pad = pad
        slots = len(self.keys) + (0.75 if self.coords else 0)
        self.row_h = (h - 2 * pad) / max(1.0, slots)
        self.val_px, self.lab_px, self.crd_px = self.row_h * 0.62, self.row_h * 0.3, self.row_h * 0.75 * 0.42
        inner = w - 2 * pad
        while self.keys and self.val_px > 6:
            fv, fl = font(self.val_px, 700), font(self.lab_px, 600)
            need = max(fl.getlength(self.LABELS[k]) + self.val_px * 0.6 + self._field_width(k, fv, fl) for k in self.keys)
            if need <= inner:
                break
            self.val_px *= 0.93
            self.lab_px *= 0.93
        while self.coords and self.crd_px > 6 and font(self.crd_px, 500).getlength(self.COORDS_TEMPLATE) > inner:
            self.crd_px *= 0.92
        self.top = pad + max(0.0, (h - 2 * pad - self.row_h * slots) / 2)

    # ------------------------------------------------------------ values
    def values(self, cur: Sample, dist: float, t: float) -> tuple:
        vals = []
        for k in self.keys:
            if k == "speed":
                v, u = units.speed(cur.speed, self.units)
            elif k == "altitude":
                v, u = units.altitude(cur.alt, self.units)
            elif k == "vspeed":
                v, u = units.vspeed(cur.vs, self.units)
            elif k == "heading":
                v, u = units.heading(cur.yaw)
            elif k == "distance":
                v, u = units.distance(dist, self.units)
            elif k == "home":
                v, u = units.distance(haversine(self.home.lat, self.home.lon, cur.lat, cur.lon), self.units)
            else:
                v, u = units.elapsed(t)
            vals.append((self.LABELS[k], v, u))
        return tuple(vals), (units.coords(cur.lat, cur.lon) if self.coords else None)

    # ------------------------------------------------------------ render
    def render(self, t: float) -> Image.Image:
        cur, _, dist = self.track.at(t)
        key = self.values(cur, dist, t)
        with self._lock:
            if key == self._cache_key and self._cache is not None:
                return self._cache
        vals, crd = key
        img = self.panel.copy()
        d = ImageDraw.Draw(img)
        with TEXT_LOCK:
            if self.column:
                self._draw_column(d, vals, crd)
            else:
                self._draw_row(d, vals, crd)
        out = self.shadow.copy()
        out.alpha_composite(img, (self.margin, self.margin))
        with self._lock:
            self._cache_key, self._cache = key, out
        return out

    def _draw_row(self, d, vals, crd) -> None:
        _, c_lab, c_val, c_unit = self.colors
        fl, fv, fc = font(self.lab_px, 600), font(self.val_px, 700), font(self.crd_px, 500)
        for i, (lab, val, unit) in enumerate(vals):
            x = self.pad + (i % self.cols) * self.colw
            y = self.top + (i // self.cols) * self.row_step
            d.text((x, y), lab, font=fl, fill=c_lab)
            vy = y + self.lab_px + self.gap
            d.text((x, vy), val, font=fv, fill=c_val)
            if unit:
                base = vy + fv.getbbox(val)[3]
                d.text((x + d.textlength(val, font=fv) + self.val_px * 0.1, base), unit, font=fl, fill=c_unit, anchor="ls")
        if crd:
            d.text((self.pad, self.top + self.n_rows * self.row_step), crd, font=fc, fill=c_lab)

    def _draw_column(self, d, vals, crd) -> None:
        _, c_lab, c_val, c_unit = self.colors
        fl, fv, fc = font(self.lab_px, 600), font(self.val_px, 700), font(self.crd_px, 500)
        right = self.w - self.pad
        for i, (lab, val, unit) in enumerate(vals):
            mid = self.top + (i + 0.5) * self.row_h
            base = mid + self.val_px * 0.36
            d.text((self.pad, base), lab, font=fl, fill=c_lab, anchor="ls")
            x = right
            if unit:
                d.text((x, base), unit, font=fl, fill=c_unit, anchor="rs")
                x -= d.textlength(unit, font=fl) + self.val_px * 0.1
            d.text((x, base), val, font=fv, fill=c_val, anchor="rs")
        if crd:
            mid = self.top + (len(vals) + 0.375) * self.row_h
            d.text((self.pad, mid), crd, font=fc, fill=c_lab, anchor="lm")
