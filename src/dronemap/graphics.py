"""Drawing primitives: fonts, colours, anti-aliased shapes, shadows, map tile styles."""

from __future__ import annotations

import functools
import os
import sys
import threading
from typing import Callable, Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

RGB = tuple[int, int, int]

# FreeType font objects aren't safe to use from several threads at once, and widgets are
# rendered on a thread pool. Every text draw/measure that can run concurrently takes this.
TEXT_LOCK = threading.RLock()
AA = 4  # supersampling factor for static shape edges (rounded corners, rings, badges)

_FONTS = {
    "darwin": {w: ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/HelveticaNeue.ttc"] for w in (500, 600, 700, 800)},
    "win32": {
        800: ["C:/Windows/Fonts/segoeuib.ttf"],
        700: ["C:/Windows/Fonts/segoeuib.ttf"],
        600: ["C:/Windows/Fonts/seguisb.ttf"],
        500: ["C:/Windows/Fonts/segoeui.ttf"],
    },
    "linux": {
        w: [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
        ]
        for w in (600, 700, 800)
    },
}
_FONTS["linux"][500] = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
]


@functools.lru_cache(maxsize=128)
def font(size: float, weight: int = 700) -> ImageFont.FreeTypeFont:
    """System UI font at `size` px and `weight` (500-800), with a built-in fallback."""
    size = max(6, int(round(size)))
    plat = sys.platform if sys.platform in ("win32", "darwin") else "linux"
    table = _FONTS[plat]
    weight = min(table, key=lambda w: abs(w - weight))
    for path in table[weight]:
        if not os.path.exists(path):
            continue
        try:
            f = ImageFont.truetype(path, size)
        except OSError:
            continue
        _set_variation(f, weight, size)
        return f
    return ImageFont.load_default(size)


def _set_variation(f: ImageFont.FreeTypeFont, weight: int, size: int) -> None:
    """Variable fonts (SF Pro): set weight and optical size, leave other axes at default."""
    try:
        axes = f.get_variation_axes()
    except Exception:  # not a variable font
        return
    vals = []
    for ax in axes:
        name = ax["name"].decode() if isinstance(ax["name"], bytes) else ax["name"]
        v = ax["default"]
        if name == "Weight":
            v = weight
        elif name == "Optical Size":
            v = min(ax["maximum"], max(ax["minimum"], size / 2))
        vals.append(v)
    try:
        f.set_variation_by_axes(vals)
    except Exception:
        pass


def hex_rgb(h: str, default: RGB = (255, 92, 54)) -> RGB:
    h = (h or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return default


def rounded_mask(w: int, h: int, r: float, inset: float = 0.0) -> Image.Image:
    """Anti-aliased rounded-rectangle alpha mask."""
    m = Image.new("L", (w * AA, h * AA), 0)
    i = inset * AA
    ImageDraw.Draw(m).rounded_rectangle([i, i, w * AA - 1 - i, h * AA - 1 - i], max(0.0, r - inset) * AA, fill=255)
    return m.reduce(AA)


def rounded_ring(w: int, h: int, r: float, width: float) -> Image.Image:
    """Anti-aliased border ring for a rounded rectangle."""
    return ImageChops.subtract(rounded_mask(w, h, r), rounded_mask(w, h, r, inset=width))


def shadow_canvas(mask: Image.Image, margin: int, strength: int) -> Image.Image:
    """Transparent canvas `margin` px larger than `mask` on every side, holding a soft
    drop shadow of the mask's shape. Built once per widget, then copied per frame."""
    w, h = mask.size
    out = Image.new("RGBA", (w + 2 * margin, h + 2 * margin), (0, 0, 0, 0))
    if margin > 0 and strength > 0:
        sh = Image.new("L", out.size, 0)
        sh.paste(mask.point(lambda a: a * strength // 255), (margin, margin + margin // 3))
        out.putalpha(sh.filter(ImageFilter.GaussianBlur(margin / 2.2)))
    return out


def downsample(img: Image.Image, factor: int) -> Image.Image:
    """Box-filter an RGBA layer by an integer factor, premultiplied so that
    semi-transparent edges don't pick up dark fringes."""
    if factor == 1:
        return img
    return img.convert("RGBa").reduce(factor).convert("RGBA")


def tile_style(style: str) -> Optional[Callable[[Image.Image], Image.Image]]:
    """Per-tile colour transform for the map styles."""
    if style == "grayscale":
        return lambda im: ImageOps.autocontrast(ImageOps.grayscale(im), cutoff=1).convert("RGB")
    if style == "dark":

        def dark(im: Image.Image) -> Image.Image:
            h, s, v = ImageOps.invert(im.convert("RGB")).convert("HSV").split()
            out = Image.merge("HSV", (h.point(lambda x: (x + 128) % 256), s, v)).convert("RGB")
            return out.point(lambda x: int(x * 0.82))

        return dark
    return None
