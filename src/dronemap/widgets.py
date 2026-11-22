"""The two on-screen widgets: the live map and the flight-stats panel.

Both are safe to render from several threads at once: per-frame work only touches
local images, and the shared caches are guarded by a lock.
"""

from __future__ import annotations

import math
import threading
from bisect import bisect_right
from collections.abc import Sequence
from typing import Optional

from PIL import Image, ImageDraw

from . import units
from .graphics import TEXT_LOCK, downsample, font, hex_rgb, rounded_mask, rounded_ring, shadow_canvas, tile_style
from .telemetry import Sample, Track
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
        tiles_url: str,
        credit: str,
        w: int,
        h: int,
        px_scale: float,
        offline: bool = False,
    ):
        self.track, self.cfg = track, cfg
        self.w, self.h = max(16, int(w)), max(16, int(h))
        self.heading_up = cfg.get("orientation") == "heading"
        self.show_path = bool(cfg.get("show_path", True))
        self.show_route = bool(cfg.get("show_full_route", False))
        self.show_cone = bool(cfg.get("show_cone", True))
        self.show_north = bool(cfg.get("show_north", True))
        self.accent = hex_rgb(style.get("accent", "#ff5c36"))

        # Pick the tile zoom whose pixels land closest to 1 screen px: same ground area
        # as the chosen zoom, but sharp at 8K and without shimmering at 1080p.
        z, k = int(cfg.get("zoom", 17)), float(px_scale)
        while k < 0.71 and z > 3:
            z, k = z - 1, k * 2
        while k > 1.42 and z < 19:
            z, k = z + 1, k / 2
        self.z, self.k = z, k
        post = tile_style(cfg.get("style", "standard"))
        self.tiles = PlaceholderTiles(z, post) if offline or tiles_url == "placeholder" else TileSource(tiles_url, z, post=post)

        self.world = [world_px(s.lat, s.lon, z) for s in track.samples]
        self.path_idx, self.path_pts = _simplify(self.world, self.PATH_STEP / k)
        self.route_pts = _simplify(self.world, 3 * self.PATH_STEP / k)[1]

        m = min(self.w, self.h)
        self.S = m / 620.0  # design units: the layout was drawn for a 620 px map
        self.radius = int(m * float(style.get("roundness", 0.06)))
        self.margin = int(max(4, m * 0.045))
        self.mask = rounded_mask(self.w, self.h, self.radius)
        ring = rounded_ring(self.w, self.h, self.radius, max(1.0, 5 * self.S))
        self.border = Image.new("RGBA", (self.w, self.h), PANEL_RGB + (0,))
        self.border.putalpha(ring.point(lambda a: a * 200 // 255))
        self.shadow = shadow_canvas(self.mask, self.margin, 110)
        self.credit = self._credit_layer(credit) if cfg.get("show_credit") and credit else None
        self.north = self._north_badge(None) if self.show_north and not self.heading_up else None

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

        # 1) Map imagery: one affine resample does the sub-pixel shift, the scale and
        #    (heading-up) the rotation, straight at output size.
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
            dr.pieslice([hx - cone, hy - cone, hx + cone, hy + cone], screen_yaw - 128, screen_yaw - 52, fill=(255, 255, 255, 80))
        pad = 40 * S * SS
        if self.show_route and len(self.route_pts) > 1:
            for run in _visible_runs([P(p) for p in self.route_pts], w * SS, h * SS, pad):
                dr.line(run, fill=(255, 255, 255, 150), width=max(2, int(6 * S * SS)), joint="curve")
        halo = 46 * S * SS
        dr.ellipse([hx - halo, hy - halo, hx + halo, hy + halo], fill=self.accent + (70,))
        if self.show_path:
            n = bisect_right(self.path_idx, idx)
            pts = [P(p) for p in self.path_pts[:n]] + [(hx, hy)]
            for run in _visible_runs(pts, w * SS, h * SS, pad):
                if len(run) > 1:
                    dr.line(run, fill=(255, 255, 255, 255), width=max(3, int(15 * S * SS)), joint="curve")
                    dr.line(run, fill=self.accent + (255,), width=max(2, int(9 * S * SS)), joint="curve")
            sx, sy = P(self.world[0])
            r = 11 * S * SS
            dr.ellipse(
                [sx - r, sy - r, sx + r, sy + r], fill=(255, 255, 255), outline=(30, 30, 30), width=max(1, int(3 * S * SS))
            )
        ya = math.radians(screen_yaw)
        cy_, sy_ = math.cos(ya), math.sin(ya)

        def R(x: float, y: float) -> Point:
            return hx + x * cy_ - y * sy_, hy + x * sy_ + y * cy_

        L = 30 * S * SS
        dr.polygon([R(0, -L), R(0.72 * L, 0.78 * L), R(0, 0.38 * L), R(-0.72 * L, 0.78 * L)], fill=(255, 255, 255))
        dr.polygon([R(0, -0.62 * L), R(0.42 * L, 0.5 * L), R(0, 0.22 * L), R(-0.42 * L, 0.5 * L)], fill=self.accent)
        img.alpha_composite(downsample(layer, SS))

        # 3) Static decorations, rounded corners, border, shadow.
        if self.show_north:
            badge = self.north if self.north is not None else self._north_badge(yaw)
            nr = badge.width // 2
            img.alpha_composite(badge, (w - nr - int(18 * S) - nr, int(18 * S)))
        if self.credit is not None:
            img.alpha_composite(self.credit)
        img.putalpha(self.mask)
        img.alpha_composite(self.border)
        out = self.shadow.copy()
        out.alpha_composite(img, (self.margin, self.margin))
        return out

    def _north_badge(self, yaw: Optional[float]) -> Image.Image:
        """N badge; in heading-up mode a needle points to true north."""
        nr = max(8, int(26 * self.S))
        D = 2 * nr * 4
        big = Image.new("RGBA", (D, D), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        c, R = D / 2, nr * 4
        d.ellipse([0, 0, D - 1, D - 1], fill=PANEL_RGB + (215,))
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

    def _credit_layer(self, credit: str) -> Image.Image:
        layer = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        S = self.S
        ImageDraw.Draw(layer).text(
            (self.w - int(14 * S), self.h - int(10 * S)),
            credit,
            font=font(max(10, 16 * S), 500),
            fill=(40, 40, 40),
            anchor="rd",
            stroke_width=max(1, int(2 * S)),
            stroke_fill=(255, 255, 255),
        )
        return layer


class StatsWidget:
    """Panel with speed / altitude / heading / distance and optional coordinates."""

    # Widest value each column must fit, so the font size never jumps mid-video.
    TEMPLATES = {
        "metric": {"speed": ("188", "km/h"), "altitude": ("888", "m"), "heading": ("888", "°"), "distance": ("8.88", "km")},
        "imperial": {"speed": ("188", "mph"), "altitude": ("1888", "ft"), "heading": ("888", "°"), "distance": ("8.88", "mi")},
    }
    KEYS = ("speed", "altitude", "heading", "distance")
    LABELS = {"speed": "SPEED", "altitude": "ALTITUDE", "heading": "HEADING", "distance": "DISTANCE"}

    def __init__(self, track: Track, cfg: dict, style: dict, w: int, h: int):
        self.track = track
        self.w, self.h = max(16, int(w)), max(16, int(h))
        self.units = "imperial" if style.get("units") == "imperial" else "metric"
        self.keys = [k for k in self.KEYS if cfg.get(k)]
        self.coords = bool(cfg.get("coords"))
        self.radius = int(min(self.w, self.h) * float(style.get("roundness", 0.06)) * 1.6)
        self.margin = int(max(4, min(self.w, self.h * 3) * 0.02))
        self.mask = rounded_mask(self.w, self.h, self.radius)
        alpha = int(255 * max(0.0, min(1.0, float(style.get("panel_opacity", 0.8)))))
        self.panel = Image.new("RGBA", (self.w, self.h), PANEL_RGB + (0,))
        self.panel.putalpha(self.mask.point(lambda a: a * alpha // 255))
        self.shadow = shadow_canvas(self.mask, self.margin, 90 * alpha // 255)
        self._layout()
        self._lock = threading.Lock()
        self._cache_key: Optional[tuple] = None
        self._cache: Optional[Image.Image] = None

    @classmethod
    def has_content(cls, cfg: dict) -> bool:
        return any(cfg.get(k) for k in cls.KEYS) or bool(cfg.get("coords"))

    def _layout(self) -> None:
        w, h = self.w, self.h
        pad = min(w, h) * 0.16
        self.pad = pad
        self.colw = (w - 2 * pad) / max(1, len(self.keys))
        avail = h - 2 * pad
        rows = ([0.24, 0.62] if self.keys else []) + ([0.2] if self.coords else [])
        unit = avail / (sum(rows) + 0.08 * max(0, len(rows) - 1)) if rows else 1.0
        self.lab_px, self.val_px, self.crd_px, self.gap = 0.24 * unit, 0.62 * unit, 0.2 * unit, 0.08 * unit
        tpl = self.TEMPLATES[self.units]
        while self.keys and self.val_px > 6:
            fv, fl = font(self.val_px, 700), font(self.lab_px, 600)
            widest = max(fv.getlength(tpl[k][0]) + self.val_px * 0.1 + fl.getlength(tpl[k][1]) for k in self.keys)
            if widest <= self.colw * 0.94:
                break
            self.val_px *= 0.92
        self.lab_px = min(self.lab_px, self.val_px * 0.42)
        widest_coords = "88.88888° N   888.88888° W"
        while self.coords and self.crd_px > 6 and font(self.crd_px, 500).getlength(widest_coords) > w - 2 * pad:
            self.crd_px *= 0.92
        block = (self.lab_px + self.gap + self.val_px if self.keys else 0) + (
            (self.gap if self.keys else 0) + self.crd_px if self.coords else 0
        )
        self.top = pad + max(0.0, (avail - block) / 2)

    def values(self, cur: Sample, dist: float) -> tuple:
        vals = []
        for k in self.keys:
            if k == "speed":
                v, u = units.speed(cur.speed, self.units)
            elif k == "altitude":
                v, u = units.altitude(cur.alt, self.units)
            elif k == "heading":
                v, u = units.heading(cur.yaw)
            else:
                v, u = units.distance(dist, self.units)
            vals.append((self.LABELS[k], v, u))
        return tuple(vals), (units.coords(cur.lat, cur.lon) if self.coords else None)

    def render(self, t: float) -> Image.Image:
        cur, _, dist = self.track.at(t)
        key = self.values(cur, dist)
        with self._lock:
            if key == self._cache_key and self._cache is not None:
                return self._cache
        vals, crd = key
        img = self.panel.copy()
        d = ImageDraw.Draw(img)
        y = self.top
        with TEXT_LOCK:
            fl, fv, fc = font(self.lab_px, 600), font(self.val_px, 700), font(self.crd_px, 500)
            for i, (lab, val, unit) in enumerate(vals):
                x = self.pad + i * self.colw
                d.text((x, y), lab, font=fl, fill=(150, 158, 170))
                vy = y + self.lab_px + self.gap
                d.text((x, vy), val, font=fv, fill=(255, 255, 255))
                base = vy + fv.getbbox(val)[3]
                ux = x + d.textlength(val, font=fv) + self.val_px * 0.1
                d.text((ux, base), unit, font=fl, fill=(200, 205, 212), anchor="ls")
            if crd:
                cy = y + ((self.lab_px + self.gap + self.val_px + self.gap) if vals else 0)
                d.text((self.pad, cy), crd, font=fc, fill=(150, 158, 170))
        out = self.shadow.copy()
        out.alpha_composite(img, (self.margin, self.margin))
        with self._lock:
            self._cache_key, self._cache = key, out
        return out
