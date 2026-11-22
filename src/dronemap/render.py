"""Draw the map and stats widgets for any moment of a flight."""
from __future__ import annotations

import functools
import math
import os
import sys
from typing import Optional, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from .telemetry import Sample, Track
from .tiles import PlaceholderTiles, TileSource, tiles_along, world_px

# ------------------------------------------------------------------ fonts
_FONT_CANDIDATES = {
    "darwin": ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/HelveticaNeue.ttc"],
    "win32": {700: ["C:/Windows/Fonts/segoeuib.ttf"], 600: ["C:/Windows/Fonts/seguisb.ttf"],
              500: ["C:/Windows/Fonts/segoeui.ttf"]},
    "linux": {700: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"],
              500: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    "/usr/share/fonts/TTF/DejaVuSans.ttf",
                    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"]},
}


@functools.lru_cache(maxsize=64)
def font(size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
    size = max(6, int(size))
    plat = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    cands = _FONT_CANDIDATES[plat]
    if isinstance(cands, dict):
        cands = cands.get(weight) or cands.get(700 if weight >= 600 else 500) or []
    for path in cands:
        if not os.path.exists(path):
            continue
        try:
            f = ImageFont.truetype(path, size)
        except OSError:
            continue
        try:  # variable fonts (SF Pro): set weight, keep everything else default
            axes = f.get_variation_axes()
            vals = []
            for ax in axes:
                name = ax["name"].decode() if isinstance(ax["name"], bytes) else ax["name"]
                v = ax["default"]
                if name == "Weight":
                    v = weight
                elif name == "Optical Size":
                    v = min(ax["maximum"], max(ax["minimum"], size / 2))
                vals.append(v)
            f.set_variation_by_axes(vals)
        except Exception:
            pass
        return f
    return ImageFont.load_default(size)


# ------------------------------------------------------------------ helpers
def hex_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (255, 92, 54)


def style_post(style: str):
    if style == "grayscale":
        return lambda im: ImageOps.autocontrast(ImageOps.grayscale(im), cutoff=1).convert("RGB")
    if style == "dark":
        def dark(im):
            inv = ImageOps.invert(im.convert("RGB"))
            h, s, v = inv.convert("HSV").split()
            h = h.point(lambda x: (x + 128) % 256)
            out = Image.merge("HSV", (h, s, v)).convert("RGB")
            return out.point(lambda x: int(x * 0.82))
        return dark
    return None


AA = 4  # supersampling factor for shape edges (rounded corners, rings, badges)


def rounded_mask(w: int, h: int, r: float, inset: float = 0.0) -> Image.Image:
    """Anti-aliased rounded-rectangle alpha mask (drawn at AA x size, then downsampled)."""
    m = Image.new("L", (w * AA, h * AA), 0)
    i = inset * AA
    ImageDraw.Draw(m).rounded_rectangle([i, i, w * AA - 1 - i, h * AA - 1 - i],
                                        max(0.0, (r - inset)) * AA, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def rounded_ring(w: int, h: int, r: float, width: float) -> Image.Image:
    """Anti-aliased border ring for a rounded rectangle."""
    return ImageChops.subtract(rounded_mask(w, h, r), rounded_mask(w, h, r, inset=width))


def aa_disc(size: int, color, ring=None) -> Image.Image:
    """Anti-aliased filled circle as an RGBA image of size x size."""
    big = Image.new("RGBA", (size * AA, size * AA), (0, 0, 0, 0))
    ImageDraw.Draw(big).ellipse([0, 0, size * AA - 1, size * AA - 1], fill=color)
    return big.resize((size, size), Image.LANCZOS)


def with_shadow(img: Image.Image, mask: Image.Image, margin: int, strength: int = 110) -> Image.Image:
    """Return img (RGBA) on a transparent canvas grown by `margin`, with a soft drop shadow."""
    w, h = img.size
    out = Image.new("RGBA", (w + 2 * margin, h + 2 * margin), (0, 0, 0, 0))
    if margin > 0:
        sh = Image.new("L", out.size, 0)
        sh.paste(mask.point(lambda a: a * strength // 255), (margin, margin + margin // 3))
        sh = sh.filter(ImageFilter.GaussianBlur(margin / 2.2))
        out.putalpha(sh)
    out.alpha_composite(img, (margin, margin))
    return out


# ------------------------------------------------------------------ units
def fmt_speed(mps, units):
    if mps is None:
        return "--", ""
    return ("%.0f" % (mps * 2.236936), "mph") if units == "imperial" else ("%.0f" % (mps * 3.6), "km/h")


def fmt_alt(m, units):
    if m is None:
        return "--", ""
    return ("%.0f" % (m * 3.28084), "ft") if units == "imperial" else ("%.0f" % m, "m")


def fmt_dist(m, units):
    if units == "imperial":
        ft = m * 3.28084
        return ("%.0f" % ft, "ft") if ft < 1000 else ("%.2f" % (m / 1609.344), "mi")
    return ("%.0f" % m, "m") if m < 1000 else ("%.2f" % (m / 1000), "km")


# ------------------------------------------------------------------ widgets
class MapWidget:
    SS = 2  # supersampling for smooth lines

    def __init__(self, track: Track, cfg: dict, style: dict, tiles_url: str, credit: str,
                 w: int, h: int, px_scale: float, offline: bool = False):
        self.track, self.cfg, self.style = track, cfg, style
        self.w, self.h = max(16, int(w)), max(16, int(h))
        self.z = int(cfg.get("zoom", 17))
        self.k = px_scale              # screen px per tile px
        self.credit = credit
        post = style_post(cfg.get("style", "standard"))
        if offline or tiles_url == "placeholder":
            self.tiles = PlaceholderTiles(self.z, post)
        else:
            self.tiles = TileSource(tiles_url, self.z, post=post)
        self.world = [world_px(s.lat, s.lon, self.z) for s in track.samples]
        self.accent = hex_rgb(style.get("accent", "#ff5c36"))
        m = min(self.w, self.h)
        self.S = m / 620.0
        self.radius = int(m * float(style.get("roundness", 0.06)))
        self.mask = rounded_mask(self.w, self.h, self.radius)
        ring = rounded_ring(self.w, self.h, self.radius, max(1.0, 5 * self.S))
        self.border = Image.new("RGBA", (self.w, self.h), (14, 17, 22, 0))
        self.border.putalpha(ring.point(lambda a: a * 200 // 255))
        self.margin = int(max(4, m * 0.045))

    def view_radius_px(self) -> float:
        return math.hypot(self.w, self.h) / self.k / 2 + 8

    def prefetch(self, progress=None, cancelled=None):
        self.tiles.prefetch(tiles_along(self.world, self.view_radius_px()), progress, cancelled)

    def render(self, t: float) -> Image.Image:
        cur, idx, _ = self.track.at(t)
        ss, w, h, S = self.SS, self.w, self.h, self.S
        heading_up = self.cfg.get("orientation") == "heading"
        yaw = cur.yaw or 0.0
        cx, cy = world_px(cur.lat, cur.lon, self.z)

        # Square render area big enough to survive rotation in heading-up mode.
        side = int(math.ceil(math.hypot(w, h))) if heading_up else None
        rw, rh = (side, side) if heading_up else (w, h)
        vw, vh = rw / self.k, rh / self.k
        x0, y0 = cx - vw / 2, cy - vh / 2
        region, ox, oy = self.tiles.region(x0, y0, x0 + vw, y0 + vh)
        # Float crop box: the map moves by fractions of a pixel, so scrolling stays smooth.
        big = region.resize((rw * ss, rh * ss), Image.BICUBIC,
                            box=(x0 - ox, y0 - oy, x0 - ox + vw, y0 - oy + vh)).convert("RGBA")
        kk = self.k * ss
        rot = math.radians(yaw) if heading_up else 0.0
        ca, sa = math.cos(rot), math.sin(rot)
        hx, hy = rw * ss / 2, rh * ss / 2

        def P(wx, wy):
            dx, dy = (wx - cx) * kk, (wy - cy) * kk
            # rotate so the heading points up (image is rotated counter-clockwise by yaw)
            return hx + dx * ca + dy * sa, hy - dx * sa + dy * ca

        if heading_up:
            big = big.rotate(yaw, resample=Image.BICUBIC, center=(hx, hy))

        fx = Image.new("RGBA", big.size, (0, 0, 0, 0))
        dfx = ImageDraw.Draw(fx)
        screen_yaw = 0.0 if heading_up else yaw
        if self.cfg.get("show_cone", True):
            cone = 210 * S * ss
            dfx.pieslice([hx - cone, hy - cone, hx + cone, hy + cone],
                         screen_yaw - 90 - 38, screen_yaw - 90 + 38, fill=(255, 255, 255, 80))
        if self.cfg.get("show_full_route", False) and len(self.world) > 1:
            dfx.line([P(*p) for p in self.world[::2]] + [P(*self.world[-1])],
                     fill=(255, 255, 255, 150), width=max(2, int(6 * S * ss)), joint="curve")
        halo = 46 * S * ss
        dfx.ellipse([hx - halo, hy - halo, hx + halo, hy + halo], fill=self.accent + (70,))
        big.alpha_composite(fx)

        d = ImageDraw.Draw(big)
        if self.cfg.get("show_path", True):
            pts = [P(*p) for p in self.world[:idx + 1]] + [(hx, hy)]
            if len(pts) > 1:
                d.line(pts, fill=(255, 255, 255, 255), width=max(3, int(15 * S * ss)), joint="curve")
                d.line(pts, fill=self.accent + (255,), width=max(2, int(9 * S * ss)), joint="curve")
            sx, sy = P(*self.world[0])
            r = 11 * S * ss
            d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(255, 255, 255),
                      outline=(30, 30, 30), width=max(1, int(3 * S * ss)))

        a = math.radians(screen_yaw)

        def R(x, y):
            return hx + x * math.cos(a) - y * math.sin(a), hy + x * math.sin(a) + y * math.cos(a)

        L = 30 * S * ss
        d.polygon([R(0, -L), R(0.72 * L, 0.78 * L), R(0, 0.38 * L), R(-0.72 * L, 0.78 * L)], fill=(255, 255, 255))
        d.polygon([R(0, -L * .62), R(.42 * L, .5 * L), R(0, .22 * L), R(-.42 * L, .5 * L)], fill=self.accent)

        img = big.resize((rw, rh), Image.LANCZOS)
        if heading_up:
            l, tp = (rw - w) // 2, (rh - h) // 2
            img = img.crop((l, tp, l + w, tp + h))

        d2 = ImageDraw.Draw(img)
        if self.cfg.get("show_north", True):
            nr = max(8, int(26 * S))
            nx, ny = w - nr - int(18 * S), nr + int(18 * S)
            img.alpha_composite(self._north_badge(nr, yaw if heading_up else None), (nx - nr, ny - nr))
        if self.cfg.get("show_credit", False) and self.credit:
            d2.text((w - int(14 * S), h - int(10 * S)), self.credit, font=font(max(10, 16 * S), 500),
                    fill=(40, 40, 40), anchor="rd", stroke_width=max(1, int(2 * S)),
                    stroke_fill=(255, 255, 255))

        # rounded corners + subtle border, both anti-aliased
        img.putalpha(ImageChops.multiply(img.getchannel("A"), self.mask))
        img.alpha_composite(self.border)
        return with_shadow(img, self.mask, self.margin)

    def _north_badge(self, nr: int, yaw):
        """North marker drawn at AA x size then downsampled so its edges stay smooth."""
        key = (nr, None if yaw is None else round(yaw, 1))
        if getattr(self, "_badge_key", None) == key:
            return self._badge
        D = 2 * nr * AA
        big = Image.new("RGBA", (D, D), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        c = D / 2
        d.ellipse([0, 0, D - 1, D - 1], fill=(14, 17, 22, 215))
        R = nr * AA
        if yaw is not None:  # heading-up: needle points to true north
            na = math.radians(-yaw)
            pt = lambda a, r: (c + math.sin(a) * r, c - math.cos(a) * r)
            d.polygon([pt(na, R * 0.82), pt(na + 2.55, R * 0.5), pt(na - 2.55, R * 0.5)], fill=self.accent)
            d.text((c, c), "N", font=font(R * 0.72, 800), fill=(255, 255, 255), anchor="mm")
        else:
            d.text((c, c + AA), "N", font=font(R * 1.15, 800), fill=(255, 255, 255), anchor="mm")
        self._badge_key, self._badge = key, big.resize((2 * nr, 2 * nr), Image.LANCZOS)
        return self._badge


class StatsWidget:
    TEMPLATES = {"speed": ("188", "km/h"), "altitude": ("888", "m"), "heading": ("888", "\u00b0"),
                 "distance": ("8.88", "km")}
    TEMPLATES_IMPERIAL = {"speed": ("188", "mph"), "altitude": ("1888", "ft"), "heading": ("888", "\u00b0"),
                          "distance": ("8.88", "mi")}

    def __init__(self, track: Track, cfg: dict, style: dict, w: int, h: int):
        self.track, self.cfg, self.style = track, cfg, style
        self.w, self.h = max(16, int(w)), max(16, int(h))
        self.units = style.get("units", "metric")
        self.keys = [k for k in ("speed", "altitude", "heading", "distance") if cfg.get(k)]
        self.coords = bool(cfg.get("coords"))
        m = min(self.w, self.h * 3)
        self.radius = int(min(self.w, self.h) * float(style.get("roundness", 0.06)) * 1.6)
        self.mask = rounded_mask(self.w, self.h, self.radius)
        self.margin = int(max(4, m * 0.02))
        alpha = int(255 * max(0.0, min(1.0, float(style.get("panel_opacity", 0.8)))))
        self.bg = Image.new("RGBA", (self.w, self.h), (14, 17, 22, alpha))
        self.bg.putalpha(ImageChops.multiply(self.bg.getchannel("A"), self.mask))
        self._layout()
        self._cache_key, self._cache = None, None

    def _layout(self):
        w, h = self.w, self.h
        pad = min(w, h) * 0.16
        n = max(1, len(self.keys))
        self.pad = pad
        self.colw = (w - 2 * pad) / n
        avail = h - 2 * pad
        # relative row heights: label, value, coords
        rows = [0.24, 0.62] if self.keys else []
        if self.coords:
            rows.append(0.2)
        tot = sum(rows) + 0.08 * (len(rows) - 1) if rows else 1
        unit = avail / tot
        self.lab_px = 0.24 * unit
        self.val_px = 0.62 * unit
        self.crd_px = 0.2 * unit
        # value must fit its column (number + unit)
        while self.keys and self.val_px > 6:
            fv, fl = font(self.val_px, 700), font(self.lab_px, 600)
            tpl = self.TEMPLATES_IMPERIAL if self.units == "imperial" else self.TEMPLATES
            widest = max(fv.getlength(tpl[k][0]) + self.val_px * 0.1 + fl.getlength(tpl[k][1]) for k in self.keys)
            if widest <= self.colw * 0.94:
                break
            self.val_px *= 0.92
        self.lab_px = min(self.lab_px, self.val_px * 0.42)
        if self.coords:
            while self.crd_px > 6 and font(self.crd_px, 500).getlength("88.88888° N   888.88888° W") > w - 2 * pad:
                self.crd_px *= 0.92
        block = (self.lab_px + 0.08 * unit + self.val_px if self.keys else 0) + \
                ((0.08 * unit if self.keys else 0) + self.crd_px if self.coords else 0)
        self.top = pad + max(0, (avail - block) / 2)
        self.gap = 0.08 * unit

    def values(self, cur: Sample, dist: float):
        out = []
        for k in self.keys:
            if k == "speed":
                v, u = fmt_speed(cur.speed, self.units)
                out.append(("SPEED", v, u))
            elif k == "altitude":
                v, u = fmt_alt(cur.alt, self.units)
                out.append(("ALTITUDE", v, u))
            elif k == "heading":
                out.append(("HEADING", "--" if cur.yaw is None else "%03.0f" % (cur.yaw % 360), "\u00b0"))
            elif k == "distance":
                v, u = fmt_dist(dist, self.units)
                out.append(("DISTANCE", v, u))
        crd = None
        if self.coords:
            crd = "%.5f\u00b0 %s   %.5f\u00b0 %s" % (abs(cur.lat), "N" if cur.lat >= 0 else "S",
                                                   abs(cur.lon), "E" if cur.lon >= 0 else "W")
        return tuple(out), crd

    def render(self, t: float) -> Image.Image:
        cur, _, dist = self.track.at(t)
        key = self.values(cur, dist)
        if key == self._cache_key:
            return self._cache
        vals, crd = key
        img = self.bg.copy()
        d = ImageDraw.Draw(img)
        fl, fv, fc = font(self.lab_px, 600), font(self.val_px, 700), font(self.crd_px, 500)
        y = self.top
        for i, (lab, val, unit) in enumerate(vals):
            x = self.pad + i * self.colw
            d.text((x, y), lab, font=fl, fill=(150, 158, 170))
            vy = y + self.lab_px + self.gap
            d.text((x, vy), val, font=fv, fill=(255, 255, 255))
            vw = d.textlength(val, font=fv)
            asc_v = fv.getbbox(val)[3]
            d.text((x + vw + self.val_px * 0.1, vy + asc_v), unit, font=fl, fill=(200, 205, 212), anchor="ls")
        if crd:
            cy = y + ((self.lab_px + self.gap + self.val_px + self.gap) if vals else 0)
            d.text((self.pad, cy), crd, font=fc, fill=(150, 158, 170))
        out = with_shadow(img, self.mask, self.margin, strength=90)
        self._cache_key, self._cache = key, out
        return out


# ------------------------------------------------------------------ overlay
class Overlay:
    """All widgets for one video. `widgets` is a list of (name, widget, x, y) where
    x, y is where the widget's rendered image goes (already includes shadow margin)."""

    def __init__(self, track: Track, settings: dict, frame_w: int, frame_h: int, offline: bool = False):
        self.track, self.settings = track, settings
        self.W, self.H = frame_w, frame_h
        px_scale = min(frame_w, frame_h) / 2160.0
        st = settings["style"]
        self.widgets = []
        m = settings["map"]
        if m.get("enabled", True):
            x, y, w, h = self._px(m["box"])
            mw = MapWidget(track, m, st, settings["tiles"]["url"], settings["tiles"]["credit"],
                           w, h, px_scale, offline=offline)
            self.widgets.append(("map", mw, x - mw.margin, y - mw.margin))
        s = settings["stats"]
        if s.get("enabled", True) and (any(s.get(k) for k in StatsWidget.TEMPLATES) or s.get("coords")):
            x, y, w, h = self._px(s["box"])
            sw = StatsWidget(track, s, st, w, h)
            self.widgets.append(("stats", sw, x - sw.margin, y - sw.margin))

    def _px(self, box):
        x, y, w, h = box
        return int(round(x * self.W)), int(round(y * self.H)), int(round(w * self.W)), int(round(h * self.H))

    def prefetch(self, progress=None, cancelled=None):
        for name, w, _, _ in self.widgets:
            if name == "map":
                w.prefetch(progress, cancelled)

    def compose(self, background: Image.Image, t: float) -> Image.Image:
        bg = background.convert("RGBA").resize((self.W, self.H)) if background.size != (self.W, self.H) \
            else background.convert("RGBA")
        for _, w, x, y in self.widgets:
            img = w.render(t)
            bg.alpha_composite(img, (max(0, x), max(0, y)), (max(0, -x), max(0, -y)))
        return bg
