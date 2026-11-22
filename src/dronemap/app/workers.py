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
            self.signals.done.emit(dict(path=self.path, track=track, bg=bg, W=info.width, H=info.height, dur=info.duration, t=t))
        except Exception as e:
            log.info("Preview load failed for %s: %s", self.path, e)
            self.signals.done.emit(dict(path=self.path, error=describe(e)[0]))


class FrameJob(QRunnable):
    """Decode one frame for the preview background."""

    def __init__(self, path: str, t: float, token: int):
        super().__init__()
        self.path, self.t, self.token = path, t, token
        self.setAutoDelete(False)
        self.signals = _Signals()

    def run(self) -> None:
        try:
            self.signals.done.emit(dict(token=self.token, bg=grab_frame(self.path, self.t)))
        except Exception as e:
            log.info("Preview frame failed: %s", e)
