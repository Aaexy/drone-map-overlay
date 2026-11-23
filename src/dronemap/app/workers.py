"""Background work for the app: rendering videos and loading previews."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRunnable, QThread, Signal

from ..errors import Cancelled, describe, log_failure
from ..overlay import Overlay
from ..pipeline import grab_frame, probe, render_video
from ..telemetry import load_track

log = logging.getLogger(__name__)


class RenderWorker(QThread):
    """Renders one video. Signals are delivered on the UI thread."""

    progress = Signal(str, float)  # stage, fraction
    succeeded = Signal(object)  # RenderResult
    failed = Signal(str, str)  # message, hint ("" if none)
    cancelled = Signal()

    def __init__(self, src: str, cfg: dict):
        super().__init__()
        self.src, self.cfg = src, cfg
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            res = render_video(self.src, None, self.cfg, self.progress.emit, lambda: self._cancel)
        except Cancelled:
            log.info("Cancelled %s", self.src)
            self.cancelled.emit()
        except Exception as e:  # report it, keep the app alive
            log_failure(log, "Render failed for %s" % self.src, e)
            msg, hint = describe(e)
            self.failed.emit(msg, hint or "")
        else:
            self.succeeded.emit(res)


class _Signals(QObject):
    done = Signal(object)

    def send(self, payload: dict) -> None:
        """Deliver a result; quietly drop it if the window that asked has closed."""
        try:
            self.done.emit(payload)
        except RuntimeError:
            pass


class PreviewLoadJob(QRunnable):
    """Load a preview video's GPS, one frame and its map tiles off the UI thread."""

    def __init__(self, path: str, cfg: dict):
        super().__init__()
        self.path, self.cfg = path, cfg
        self.setAutoDelete(False)
        self.signals = _Signals()

    def run(self) -> None:
        try:
            track = load_track(self.path)
            info = probe(self.path)
            t = min(track.duration * 0.5, max(0.0, info.duration - 0.05))
            bg = grab_frame(self.path, t)
            Overlay(track, self.cfg, info.width, info.height).prefetch()
            self.signals.send(dict(path=self.path, track=track, bg=bg, W=info.width, H=info.height, dur=info.duration, t=t))
        except Exception as e:
            log.info("Preview load failed for %s: %s", self.path, e)
            self.signals.send(dict(path=self.path, error=describe(e)[0]))


class FrameJob(QRunnable):
    """Decode one frame for the preview background."""

    def __init__(self, path: str, t: float, token: int):
        super().__init__()
        self.path, self.t, self.token = path, t, token
        self.setAutoDelete(False)
        self.signals = _Signals()

    def run(self) -> None:
        try:
            self.signals.send(dict(token=self.token, bg=grab_frame(self.path, self.t)))
        except Exception as e:
            log.info("Preview frame failed: %s", e)
            self.signals.send(dict(token=self.token, error=str(e)))


class WidgetPreviewJob(QRunnable):
    """Render the widgets for the layout preview (may download map tiles, so not on the UI thread)."""

    def __init__(self, token: int, track, cfg: dict, W: int, H: int, t: float, offline: bool, scale: float, video=None):
        super().__init__()
        self.token, self.track, self.cfg, self.video = token, track, cfg, video
        self.W, self.H, self.t, self.offline, self.scale = W, H, t, offline, scale
        self.setAutoDelete(False)
        self.signals = _Signals()

    def run(self) -> None:
        from PIL import Image

        try:
            ov = Overlay(self.track, self.cfg, self.W, self.H, offline=self.offline, video=self.video)
            if ov.streets is not None:
                # instant once downloaded; lined up with the video only near this moment
                ov.streets.prefetch(around=self.t)
            items = []
            for p in ov.widgets:
                img = p.widget.render(self.t)
                size = (max(1, int(img.width * self.scale)), max(1, int(img.height * self.scale)))
                items.append((p.name, img.resize(size, Image.LANCZOS), p.widget.margin * self.scale))
            errors = [p.widget.tiles.last_error for p in ov.widgets if hasattr(p.widget, "tiles") and p.widget.tiles.failed]
            self.signals.send(
                dict(
                    token=self.token,
                    items=items,
                    tile_error=errors[0] if errors else None,
                    streets_error=ov.streets_error(),
                )
            )
        except Exception as e:
            log.info("Preview render failed: %s", e)
            self.signals.send(dict(token=self.token, error=describe(e)[0]))
