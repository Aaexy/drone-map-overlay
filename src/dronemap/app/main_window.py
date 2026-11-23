"""Main window: drop zone plus the render queue."""

from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, logs
from .. import settings as settings_mod
from .controls import MapPattern, button, label
from .platform import asset, open_file, reveal
from .settings_dialog import SettingsDialog
from .theme import T
from .workers import RenderWorker

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}
APP_TITLE = "Drone Map Overlay"
VIDEO_FILTER = "Videos (*.mp4 *.MP4 *.mov *.MOV *.m4v *.mkv);;All files (*)"


def fmt_secs(s: float) -> str:
    s = int(max(0, s))
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60) if s >= 3600 else "%d:%02d" % (s // 60, s % 60)


def videos_in(paths: list[str]) -> list[str]:
    """Video files among `paths`; folders contribute the videos directly inside them."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if os.path.splitext(name)[1].lower() in VIDEO_EXT and not name.startswith("._"):
                    files.append(os.path.join(p, name))
        elif os.path.splitext(p)[1].lower() in VIDEO_EXT and not os.path.basename(p).startswith("._"):
            files.append(p)
    return files


class DropZone(QFrame):
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.art = MapPattern(self)

    def resizeEvent(self, e) -> None:
        self.art.setGeometry(self.rect())
        self.art.lower()
        super().resizeEvent(e)

    def set_hover(self, on: bool) -> None:
        self.art.hover = on
        self.art.update()

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.set_hover(True)

    def dragLeaveEvent(self, e) -> None:
        self.set_hover(False)

    def dropEvent(self, e) -> None:
        self.set_hover(False)
        files = videos_in([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])
        if files:
            self.files_dropped.emit(files)


class JobRow(QFrame):
    """One video in the queue."""

    remove_requested = Signal(object)
    retry_requested = Signal(object)
    cancel_requested = Signal(object)

    def __init__(self, src: str):
        super().__init__()
        self.src = src
        self.state = "queued"
        self.output = ""
        self.started = 0.0
        self.setProperty("role", "card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = QLabel("●")
        top.addWidget(self.dot)
        self.name = label(os.path.basename(src))
        self.name.setToolTip(src)
        f = self.name.font()
        f.setBold(True)
        self.name.setFont(f)
        top.addWidget(self.name, 1)
        self.pct = label("", "pct")
        top.addWidget(self.pct)
        self.btn_show = button("Show in folder", None, lambda: reveal(self.output))
        self.btn_play = button("Play", None, lambda: open_file(self.output))
        self.btn_retry = button("Retry", None, lambda: self.retry_requested.emit(self))
        self.btn_cancel = button("Cancel", "ghost", lambda: self.cancel_requested.emit(self))
        self.btn_remove = button("✕", "ghost", lambda: self.remove_requested.emit(self))
        self.btn_remove.setToolTip("Remove from the list")
        self.btn_remove.setAccessibleName("Remove from the list")
        for b in (self.btn_show, self.btn_play, self.btn_retry, self.btn_cancel, self.btn_remove):
            top.addWidget(b)
        lay.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)
        self.status = label("", "muted", wrap=True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.status)
        self.set_state("queued", "Waiting…")

    def set_state(self, state: str, text: str = "") -> None:
        self.state = state
        colors = {
            "queued": T["faint"],
            "running": T["accent"],
            "done": T["success"],
            "failed": "#ff453a",
            "cancelled": T["faint"],
        }
        self.dot.setStyleSheet("color: %s; font-size: 12px;" % colors.get(state, T["faint"]))
        self.bar.setVisible(state == "running")
        self.pct.setVisible(state == "running")
        self.btn_show.setVisible(state == "done")
        self.btn_play.setVisible(state == "done")
        self.btn_retry.setVisible(state in ("failed", "cancelled"))
        self.btn_cancel.setVisible(state == "running")
        self.btn_remove.setVisible(state != "running")
        if text:
            self.status.setText(text)

    def on_progress(self, stage: str, f: float) -> None:
        if stage == "telemetry":
            self.status.setText("Reading GPS data…")
            self.bar.setRange(0, 0)
            self.pct.setText("")
        elif stage == "map":
            self.status.setText("Downloading map tiles…")
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(f * 1000))
        elif stage == "streets":
            self.status.setText("Downloading street names…")
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(f * 1000))
        elif stage == "align":
            self.status.setText("Lining up the streets with the video…")
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(f * 1000))
        elif stage == "finishing":
            self.status.setText("Finishing up…")
            self.bar.setRange(0, 0)
            self.pct.setText("")
        elif stage == "saving":
            self.status.setText("Copying to the destination folder…")
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(f * 1000))
            self.pct.setText("%d%%" % (f * 100))
        else:
            if not self.started:
                self.started = time.time()
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(f * 1000))
            self.pct.setText("%d%%" % (f * 100))
            el = time.time() - self.started
            left = el / f - el if f > 0.02 and el > 2 else None
            self.status.setText("Rendering" + (" · about %s left" % fmt_secs(left) if left is not None else "…"))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(560, 520)
        self.resize(680, 640)
        self.rows: list[JobRow] = []
        self.worker: Optional[RenderWorker] = None
        self.current: Optional[JobRow] = None

        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(22, 18, 22, 22)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(10)
        ic = asset("icon.png")
        if ic:
            logo = QLabel()
            logo.setPixmap(
                QPixmap(ic).scaled(26, 26, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            )
            head.addWidget(logo)
        head.addWidget(label(APP_TITLE, "wordmark"))
        head.addWidget(label("v" + __version__, "chip"))
        head.addStretch(1)
        log_btn = button("Log", "ghost", lambda: reveal(logs.log_file()))
        log_btn.setToolTip("Show the log file (useful when reporting a problem)")
        head.addWidget(log_btn)
        head.addWidget(button("⚙  Settings", "ghost", self.open_settings))
        lay.addLayout(head)

        self.drop = DropZone()
        self.drop.files_dropped.connect(self.add_files)
        dl = QVBoxLayout(self.drop)
        dl.setContentsMargins(28, 24, 28, 24)
        dl.setSpacing(8)
        dl.addStretch(1)
        self.big_icon = QLabel()
        if ic:
            self.big_icon.setPixmap(
                QPixmap(ic).scaled(84, 84, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            )
        self.big_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(self.big_icon)
        t = label("Drop your drone footage", "h1")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(t)
        self.sub = label("You get a copy with a live map, speed, altitude and heading burned in.", "muted", wrap=True)
        self.sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(self.sub)
        dl.addSpacing(10)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button("Choose videos…", "primary", self.open_dialog))
        row.addStretch(1)
        dl.addLayout(row)
        dl.addSpacing(6)
        self.foot = label("MP4 · MOV  —  GPS from DJI's built-in telemetry or a .SRT file", "faint")
        self.foot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(self.foot)
        dl.addStretch(1)
        lay.addWidget(self.drop, 3)

        self.queue_head = QHBoxLayout()
        self.queue_title = label("QUEUE", "section")
        self.queue_head.addWidget(self.queue_title)
        self.queue_head.addStretch(1)
        self.clear_btn = button("Clear finished", "ghost", self.clear_finished)
        self.queue_head.addWidget(self.clear_btn)
        lay.addLayout(self.queue_head)
        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 4, 0)
        self.list_lay.setSpacing(8)
        self.list_lay.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(self.list_host)
        lay.addWidget(self.scroll, 4)

        self.setCentralWidget(root)
        self.setAcceptDrops(True)
        self._update_layout()

    # ---------------------------------------------------------- drag anywhere
    def dragEnterEvent(self, e) -> None:
        self.drop.dragEnterEvent(e)

    def dragLeaveEvent(self, e) -> None:
        self.drop.dragLeaveEvent(e)

    def dropEvent(self, e) -> None:
        self.drop.dropEvent(e)

    # ---------------------------------------------------------- queue
    def open_dialog(self) -> None:
        cfg = settings_mod.load()
        start = os.path.dirname(cfg["app"].get("last_video") or "") or os.path.expanduser("~")
        files, _ = QFileDialog.getOpenFileNames(self, "Choose drone videos", start, VIDEO_FILTER)
        if files:
            self.add_files(files)

    def open_settings(self) -> None:
        SettingsDialog(self).exec()

    def add_files(self, files: list[str]) -> None:
        active = {r.src for r in self.rows if r.state in ("queued", "running")}
        new = [f for f in files if os.path.abspath(f) not in active]
        if not new:
            return
        cfg = settings_mod.load()
        cfg["app"]["last_video"] = new[-1]
        settings_mod.save(cfg)
        for f in new:
            row = JobRow(os.path.abspath(f))
            row.remove_requested.connect(self.remove_row)
            row.retry_requested.connect(self.retry_row)
            row.cancel_requested.connect(lambda _r: self.cancel())
            self.rows.append(row)
            self.list_lay.insertWidget(self.list_lay.count() - 1, row)
        self._update_layout()
        self.start_next()

    def start_next(self) -> None:
        if self.worker is not None:
            return
        row = next((r for r in self.rows if r.state == "queued"), None)
        if row is None:
            return
        self.current = row
        row.started = 0.0
        row.set_state("running", "Starting…")
        self.worker = RenderWorker(row.src, settings_mod.load())
        self.worker.progress.connect(row.on_progress)
        self.worker.succeeded.connect(self.on_succeeded)
        self.worker.failed.connect(self.on_failed)
        self.worker.cancelled.connect(lambda: row.set_state("cancelled", "Cancelled"))
        self.worker.finished.connect(self.on_worker_finished)
        self.scroll.ensureWidgetVisible(row)
        self.worker.start()

    def on_succeeded(self, res) -> None:
        row = self.current
        row.output = res.output
        text = "Saved as %s · took %s" % (os.path.basename(res.output), fmt_secs(res.seconds))
        for w in res.warnings:
            text += "\n⚠ " + w
        row.set_state("done", text)

    def on_failed(self, message: str, hint: str) -> None:
        self.current.set_state("failed", message + ("\n" + hint if hint else ""))

    def on_worker_finished(self) -> None:
        self.worker.deleteLater()
        self.worker = None
        self.current = None
        self._update_layout()
        self.start_next()

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.current.btn_cancel.setEnabled(False)
            self.current.status.setText("Cancelling…")

    def remove_row(self, row: JobRow) -> None:
        if row.state == "running":
            return
        self.rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._update_layout()

    def retry_row(self, row: JobRow) -> None:
        row.set_state("queued", "Waiting…")
        self.start_next()

    def clear_finished(self) -> None:
        for r in [r for r in self.rows if r.state in ("done", "failed", "cancelled")]:
            self.remove_row(r)

    def _update_layout(self) -> None:
        has = bool(self.rows)
        self.queue_title.setVisible(has)
        self.clear_btn.setVisible(any(r.state in ("done", "failed", "cancelled") for r in self.rows))
        self.scroll.setVisible(has)
        self.big_icon.setVisible(not has)
        self.foot.setVisible(not has)

    def closeEvent(self, e) -> None:
        if self.worker is not None and self.worker.isRunning():
            if (
                QMessageBox.question(self, APP_TITLE, "A video is still rendering. Quit anyway?")
                != QMessageBox.StandardButton.Yes
            ):
                e.ignore()
                return
            for r in self.rows:
                if r.state == "queued":
                    r.state = "cancelled"
            self.worker.cancel()
            self.worker.wait(10000)
        e.accept()
