"""Where the widgets go on the frame.

A box is pinned to one of nine anchor points (left/center/right x top/middle/bottom).
Its offset from that anchor and its size are measured in units of the frame's *short
side*. A square map therefore stays square, and a box pinned to the bottom-left corner
stays there, whether the video is 16:9, 9:16 (vertical), 4:3 or 1:1.

    {"anchor": ["left", "bottom"], "dx": 0.044, "dy": 0.147, "w": 0.284, "h": 0.287}
"""

from __future__ import annotations

from typing import Union

Box = dict[str, object]
PxRect = tuple[int, int, int, int]

H_ANCHORS = ("left", "center", "right")
V_ANCHORS = ("top", "middle", "bottom")
CENTER_SNAP = 0.05  # box centres this close (fraction of the frame) to the middle anchor there
MIN_SIZE = 0.02  # smallest box, in short-side units
MAX_FILL = 0.98  # a box never covers more than this fraction of the frame width/height


def _anchor(box: Box) -> tuple[str, str]:
    a = box.get("anchor") or ("left", "bottom")
    h = a[0] if a and a[0] in H_ANCHORS else "left"
    v = a[1] if len(a) > 1 and a[1] in V_ANCHORS else "bottom"
    return h, v


def box_to_px(box: Box, W: int, H: int) -> PxRect:
    """Pixel rectangle (x, y, w, h) of `box` on a W x H frame, always inside the frame."""
    S = float(min(W, H))
    w = max(MIN_SIZE, float(box.get("w", 0.25))) * S
    h = max(MIN_SIZE, float(box.get("h", 0.25))) * S
    fit = min(1.0, W * MAX_FILL / w, H * MAX_FILL / h)  # shrink uniformly if it can't fit
    w, h = w * fit, h * fit
    dx, dy = float(box.get("dx", 0.0)) * S, float(box.get("dy", 0.0)) * S
    ha, va = _anchor(box)
    x = dx if ha == "left" else (W - dx - w if ha == "right" else W / 2 + dx - w / 2)
    y = dy if va == "top" else (H - dy - h if va == "bottom" else H / 2 + dy - h / 2)
    x = min(max(0.0, x), W - w)
    y = min(max(0.0, y), H - h)
    return int(round(x)), int(round(y)), int(round(w)), int(round(h))


def px_to_box(x: float, y: float, w: float, h: float, W: float, H: float) -> Box:
    """Inverse of box_to_px: anchor to whichever of the nine points the box is nearest."""
    S = float(min(W, H))
    cx, cy = x + w / 2, y + h / 2
    if abs(cx - W / 2) < CENTER_SNAP * W:
        ha, dx = "center", (cx - W / 2) / S
    elif cx < W / 2:
        ha, dx = "left", x / S
    else:
        ha, dx = "right", (W - x - w) / S
    if abs(cy - H / 2) < CENTER_SNAP * H:
        va, dy = "middle", (cy - H / 2) / S
    elif cy < H / 2:
        va, dy = "top", y / S
    else:
        va, dy = "bottom", (H - y - h) / S
    r = lambda v: round(v, 4)
    return {"anchor": [ha, va], "dx": r(dx), "dy": r(dy), "w": r(w / S), "h": r(h / S)}


def migrate_box(box: Union[Box, list, tuple], fallback: Box) -> Box:
    """Settings v1 stored [x, y, w, h] as fractions of a 16:9 frame's width/height."""
    if isinstance(box, dict) and "anchor" in box:
        return box
    if isinstance(box, (list, tuple)) and len(box) == 4:
        try:
            x, y, w, h = (float(v) for v in box)
            W, H = 16 / 9, 1.0
            return px_to_box(x * W, y * H, w * W, h * H, W, H)
        except (TypeError, ValueError):
            pass
    return dict(fallback)
