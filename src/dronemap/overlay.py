"""Places the widgets on a frame of a given size."""

from __future__ import annotations

from typing import NamedTuple, Optional, Union

from PIL import Image

from .ar import StreetsLayer, has_camera_data
from .layout import box_to_px
from .telemetry import Track
from .widgets import MapWidget, StatsWidget

Widget = Union[MapWidget, StatsWidget, StreetsLayer]


class Placed(NamedTuple):
    name: str  # "streets", "map" or "stats" (drawn in this order)
    widget: Widget
    x: int  # where the widget's rendered image goes (shadow margin included)
    y: int


class Overlay:
    """All widgets for one video size and one set of settings."""

    def __init__(self, track: Track, settings: dict, frame_w: int, frame_h: int, offline: bool = False):
        self.track, self.settings = track, settings
        self.W, self.H = int(frame_w), int(frame_h)
        style = settings["style"]
        self.widgets: list[Placed] = []
        self.streets: Optional[StreetsLayer] = None
        st = settings.get("streets") or {}
        self.streets_missing = bool(st.get("enabled")) and not has_camera_data(track)
        if st.get("enabled") and not self.streets_missing:
            self.streets = StreetsLayer(track, st, style, self.W, self.H, offline=offline or settings["tiles"].get("offline"))
            self.widgets.append(Placed("streets", self.streets, 0, 0))
        m = settings["map"]
        if m.get("enabled", True):
            x, y, w, h = box_to_px(m["box"], self.W, self.H)
            mw = MapWidget(track, m, style, settings["tiles"], w, h, px_scale=min(self.W, self.H) / 2160.0, offline=offline)
            self.widgets.append(Placed("map", mw, x - mw.margin, y - mw.margin))
        s = settings["stats"]
        if s.get("enabled", True) and StatsWidget.has_content(s):
            x, y, w, h = box_to_px(s["box"], self.W, self.H)
            sw = StatsWidget(track, s, style, w, h)
            self.widgets.append(Placed("stats", sw, x - sw.margin, y - sw.margin))

    def prefetch(self, progress=None, cancelled=None, streets_progress=None) -> None:
        for p in self.widgets:
            if isinstance(p.widget, MapWidget):
                p.widget.prefetch(progress, cancelled)
        if self.streets is not None:
            self.streets.prefetch(streets_progress or progress, cancelled)

    def streets_error(self) -> Optional[str]:
        """Why street names can't be shown, if they were asked for."""
        if self.streets_missing:
            return "This video has no camera angle data, so street names can't be shown."
        if self.streets is not None:
            return self.streets.error
        return None

    def failed_tiles(self) -> int:
        return sum(p.widget.tiles.failed for p in self.widgets if isinstance(p.widget, MapWidget))

    def compose(self, background: Image.Image, t: float) -> Image.Image:
        """Background frame with every widget drawn on it (used for previews)."""
        bg = background.convert("RGBA")
        if bg.size != (self.W, self.H):
            bg = bg.resize((self.W, self.H))
        for p in self.widgets:
            img = p.widget.render(t)
            bg.alpha_composite(img, (max(0, p.x), max(0, p.y)), (max(0, -p.x), max(0, -p.y)))
        return bg
