"""Desktop app: drop a video in, get it back with a live map overlay."""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import time
import traceback

from PIL import Image
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QApplication, QColorDialog, QDialog, QFileDialog, QFrame,
                               QGraphicsObject, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget)

from . import __version__
from . import settings as settings_mod
from . import theme
from .encode import Cancelled, demo_background, demo_track, grab_frame, render_video, video_info
from .render import Overlay
from .telemetry import load_track
from .theme import T, MapPattern, Segmented, Toggle

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}
APP_TITLE = "Drone Map Overlay"
SWATCHES = ["#ff5c36", "#ffb020", "#3ddc97", "#22c3ff", "#8b7bff", "#ff4d8d"]


# ====================================================================== helpers
def pil_to_pixmap(img: Image.Image) -> QPixmap:
    img = img.convert("RGBA")
    q = QImage(img.tobytes("raw", "RGBA"), img.width, img.height, img.width * 4, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(q.copy())


def reveal(path: str):
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except OSError:
        pass


def open_file(path: str):
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


def asset(name: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (getattr(sys, "_MEIPASS", ""), here):
        for p in (os.path.join(base, "assets", name), os.path.join(base, "dronemap", "assets", name)):
            if base and os.path.exists(p):
                return p
    return ""


def label(text, role=None, wrap=False):
    l = QLabel(text)
    if role:
        l.setProperty("role", role)
    l.setWordWrap(wrap)
    return l


def button(text, kind=None, cb=None):
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if cb:
        b.clicked.connect(cb)
    return b


def card():
    f = QFrame()
    f.setProperty("role", "card")
    return f


def fmt_secs(s):
    s = int(max(0, s))
    return "%d:%02d" % (s // 60, s % 60)


# ====================================================================== render worker
class RenderWorker(QThread):
    progress = Signal(str, float)
    finished_one = Signal(str, str)   # src, dst
    failed_one = Signal(str, str)     # src, message

    def __init__(self, src: str, cfg: dict):
        super().__init__()
        self.src, self.cfg = src, cfg
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            dst = render_video(self.src, None, self.cfg, lambda s, f: self.progress.emit(s, f),
                               lambda: self._cancel)
            self.finished_one.emit(self.src, dst)
        except Cancelled:
            self.failed_one.emit(self.src, "Cancelled")
        except Exception as e:  # show the error, keep the app alive
            traceback.print_exc()
            self.failed_one.emit(self.src, str(e) or e.__class__.__name__)


# ====================================================================== main window
class DropZone(QFrame):
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.art = MapPattern(self)

    def resizeEvent(self, e):
        self.art.setGeometry(self.rect())
        self.art.lower()
        super().resizeEvent(e)

    def _hover(self, on):
        self.art.hover = on
        self.art.update()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._hover(True)

    def dragLeaveEvent(self, e):
        self._hover(False)

    def dropEvent(self, e):
        self._hover(False)
        files = []
        for u in e.mimeData().urls():
            if not u.isLocalFile():
                continue
            p = u.toLocalFile()
            if os.path.isdir(p):
                for name in sorted(os.listdir(p)):
                    if os.path.splitext(name)[1].lower() in VIDEO_EXT and not name.startswith("._"):
                        files.append(os.path.join(p, name))
            elif os.path.splitext(p)[1].lower() in VIDEO_EXT:
                files.append(p)
        if files:
            self.files_dropped.emit(files)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(560, 500)
        self.resize(640, 560)
        self.queue = []
        self.worker = None
        self.started_at = 0.0
        self.last_output = ""

        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(22, 18, 22, 22)
        lay.setSpacing(14)

        # header
        head = QHBoxLayout()
        head.setSpacing(10)
        ic = asset("icon.png")
        if ic:
            logo = QLabel()
            logo.setPixmap(QPixmap(ic).scaled(26, 26, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))
            head.addWidget(logo)
        head.addWidget(label(APP_TITLE, "wordmark"))
        head.addWidget(label("v" + __version__, "chip"))
        head.addStretch(1)
        self.settings_btn = button("⚙  Settings", "ghost", self.open_settings)
        head.addWidget(self.settings_btn)
        lay.addLayout(head)

        # drop zone
        self.drop = DropZone()
        self.drop.files_dropped.connect(self.add_files)
        dl = QVBoxLayout(self.drop)
        dl.setContentsMargins(28, 28, 28, 28)
        dl.setSpacing(8)
        dl.addStretch(1)
        if ic:
            big = QLabel()
            big.setPixmap(QPixmap(ic).scaled(88, 88, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation))
            big.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dl.addWidget(big)
            dl.addSpacing(8)
        t = label("Drop your drone footage", "h1")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(t)
        s = label("You get a copy with a live map, speed, altitude and heading burned in.", "muted", wrap=True)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(s)
        dl.addSpacing(14)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button("Choose video…", "primary", self.open_dialog))
        row.addStretch(1)
        dl.addLayout(row)
        dl.addSpacing(10)
        f = label("MP4 · MOV  —  GPS from DJI's built-in telemetry or a .SRT file", "faint")
        f.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(f)
        dl.addStretch(1)
        lay.addWidget(self.drop, 1)

        # progress card
        self.status = card()
        sl = QVBoxLayout(self.status)
        sl.setContentsMargins(16, 14, 16, 14)
        sl.setSpacing(8)
        r1 = QHBoxLayout()
        self.job_label = label("")
        self.job_label.setTextFormat(Qt.TextFormat.RichText)
        r1.addWidget(self.job_label, 1)
        self.queue_chip = label("", "chip")
        r1.addWidget(self.queue_chip)
        self.cancel_btn = button("Cancel", "ghost", self.cancel)
        r1.addWidget(self.cancel_btn)
        sl.addLayout(r1)
        r2 = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        r2.addWidget(self.bar, 1)
        self.pct = label("", "pct")
        self.pct.setMinimumWidth(40)
        self.pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        r2.addWidget(self.pct)
        sl.addLayout(r2)
        self.stage_label = label("", "muted")
        sl.addWidget(self.stage_label)
        lay.addWidget(self.status)

        # result card
        self.done = card()
        d2 = QHBoxLayout(self.done)
        d2.setContentsMargins(16, 12, 12, 12)
        self.done_dot = QLabel("●")
        d2.addWidget(self.done_dot)
        self.done_label = label("")
        self.done_label.setTextFormat(Qt.TextFormat.RichText)
        d2.addWidget(self.done_label, 1)
        self.show_btn = button("Show in folder", None, lambda: reveal(self.last_output))
        self.play_btn = button("Play", None, lambda: open_file(self.last_output))
        d2.addWidget(self.show_btn)
        d2.addWidget(self.play_btn)
        lay.addWidget(self.done)

        self.status.hide()
        self.done.hide()
        self.setCentralWidget(root)
        self.setAcceptDrops(True)

    # drop anywhere on the window
    def dragEnterEvent(self, e):
        self.drop.dragEnterEvent(e)

    def dragLeaveEvent(self, e):
        self.drop.dragLeaveEvent(e)

    def dropEvent(self, e):
        self.drop.dropEvent(e)

    def open_dialog(self):
        cfg = settings_mod.load()
        start = os.path.dirname(cfg["app"].get("last_video") or "") or os.path.expanduser("~")
        files, _ = QFileDialog.getOpenFileNames(self, "Choose drone video", start,
                                                "Videos (*.mp4 *.MP4 *.mov *.MOV *.m4v *.mkv);;All files (*)")
        if files:
            self.add_files(files)

    def open_settings(self):
        SettingsDialog(self).exec()

    def add_files(self, files):
        self.queue.extend(files)
        cfg = settings_mod.load()
        cfg["app"]["last_video"] = files[-1]
        settings_mod.save(cfg)
        if self.worker is None:
            self.next_job()
        else:
            self.update_queue_label()

    def update_queue_label(self):
        if self.worker is None:
            return
        self.job_label.setText("<b>%s</b>" % os.path.basename(self.worker.src))
        self.queue_chip.setText("+%d queued" % len(self.queue))
        self.queue_chip.setVisible(bool(self.queue))

    def next_job(self):
        if not self.queue:
            self.worker = None
            self.status.hide()
            return
        src = self.queue.pop(0)
        self.worker = RenderWorker(src, settings_mod.load())
        self.worker.progress.connect(self.on_progress)
        self.worker.finished_one.connect(self.on_done)
        self.worker.failed_one.connect(self.on_failed)
        self.worker.finished.connect(self.next_job)
        self.done.hide()
        self.status.show()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.pct.setText("")
        self.cancel_btn.setEnabled(True)
        self.stage_label.setText("Starting…")
        self.update_queue_label()
        self.started_at = 0.0
        self.worker.start()

    def on_progress(self, stage, f):
        if stage == "telemetry":
            self.stage_label.setText("Reading GPS data…")
            self.bar.setRange(0, 0)
            self.pct.setText("")
        elif stage == "map":
            self.stage_label.setText("Downloading map tiles…")
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(f * 1000))
            self.pct.setText("")
        else:
            if not self.started_at:
                self.started_at = time.time()
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(f * 1000))
            self.pct.setText("%d%%" % (f * 100))
            el = time.time() - self.started_at
            left = el / f - el if f > 0.02 else None
            self.stage_label.setText("Rendering" + (" · about %s left" % fmt_secs(left) if left is not None else "…"))

    def _result(self, text, color, buttons):
        self.done_dot.setStyleSheet("color: %s; font-size: 14px;" % color)
        self.done_label.setText(text)
        self.show_btn.setVisible(buttons)
        self.play_btn.setVisible(buttons)
        self.done.show()

    def on_done(self, src, dst):
        self.last_output = dst
        self._result("Saved <b>%s</b>" % os.path.basename(dst), T["success"], True)

    def on_failed(self, src, msg):
        if msg == "Cancelled":
            self._result("Cancelled <b>%s</b>" % os.path.basename(src), T["faint"], False)
            return
        self._result("Couldn't process <b>%s</b>" % os.path.basename(src), T["accent"], False)
        QMessageBox.warning(self, APP_TITLE, "Couldn't process %s\n\n%s" % (os.path.basename(src), msg))

    def cancel(self):
        if self.worker:
            self.queue.clear()
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.stage_label.setText("Cancelling…")

    def closeEvent(self, e):
        if self.worker and self.worker.isRunning():
            if QMessageBox.question(self, APP_TITLE, "A video is still rendering. Quit anyway?") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.queue.clear()
            self.worker.cancel()
            self.worker.wait(5000)
        e.accept()


# ====================================================================== settings: canvas
class BoxItem(QGraphicsObject):
    """A movable, resizable widget box on the preview canvas."""
    committed = Signal()
    HANDLE = 9

    def __init__(self, name: str, title: str, frame: QRectF):
        super().__init__()
        self.name, self.title, self.frame = name, title, frame
        self.rect = QRectF()
        self.pix = None
        self.pix_size = None   # box size the pixmap was rendered for
        self.pix_margin = 0.0  # shadow margin around the box, in preview px
        self.mode = None
        self.hover = False
        self.setAcceptHoverEvents(True)
        self.setZValue(10)

    def set_rect(self, r: QRectF):
        self.prepareGeometryChange()
        self.rect = QRectF(r)
        self.update()

    def set_pixmap(self, pix, margin):
        self.prepareGeometryChange()
        self.pix, self.pix_margin, self.pix_size = pix, margin, (self.rect.width(), self.rect.height())
        self.update()

    def boundingRect(self):
        m = max(self.pix_margin * 2, 0) + self.HANDLE * 2 + 4
        return self.rect.adjusted(-m, -m - 26, m, m)

    def paint(self, p: QPainter, opt, widget=None):
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.pix is not None and self.pix_size:
            sx = self.rect.width() / max(1e-6, self.pix_size[0])
            sy = self.rect.height() / max(1e-6, self.pix_size[1])
            m = self.pix_margin
            target = QRectF(self.rect.x() - m * sx, self.rect.y() - m * sy,
                            self.rect.width() + 2 * m * sx, self.rect.height() + 2 * m * sy)
            p.drawPixmap(target, self.pix, QRectF(self.pix.rect()))
        active = self.hover or self.mode is not None
        if not active:
            return
        acc = QColor(T["accent"])
        pen = QPen(acc, 1.5)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(self.rect, 3, 3)
        p.setBrush(QColor("white"))
        for c in self._corners():
            p.drawEllipse(QRectF(c.x() - self.HANDLE / 2, c.y() - self.HANDLE / 2, self.HANDLE, self.HANDLE))
        f = QFont()
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        text = self.title.upper()
        tw = p.fontMetrics().horizontalAdvance(text) + 16
        tag = QRectF(self.rect.x(), self.rect.y() - 24, tw, 18)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(acc)
        p.drawRoundedRect(tag, 9, 9)
        p.setPen(QColor("white"))
        p.drawText(tag, Qt.AlignmentFlag.AlignCenter, text)

    def _corners(self):
        r = self.rect
        return [r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()]

    def _corner_at(self, pos: QPointF):
        tol = self.HANDLE * 1.4
        for i, c in enumerate(self._corners()):
            if abs(pos.x() - c.x()) <= tol and abs(pos.y() - c.y()) <= tol:
                return i
        return None

    def hoverMoveEvent(self, e):
        c = self._corner_at(e.pos())
        if c is not None:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor if c in (0, 3) else Qt.CursorShape.SizeBDiagCursor)
        elif self.rect.contains(e.pos()):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()

    def hoverEnterEvent(self, e):
        self.hover = True
        self.update()

    def hoverLeaveEvent(self, e):
        self.hover = False
        self.unsetCursor()
        self.update()

    def shape(self):
        path = QPainterPath()
        h = self.HANDLE * 1.4
        path.addRect(self.rect.adjusted(-h, -h, h, h))
        return path

    def mousePressEvent(self, e):
        c = self._corner_at(e.pos())
        self.mode = ("resize", c) if c is not None else ("move", None)
        self.start_pos = e.scenePos()
        self.start_rect = QRectF(self.rect)
        e.accept()

    def mouseMoveEvent(self, e):
        if not self.mode:
            return
        d = e.scenePos() - self.start_pos
        r = QRectF(self.start_rect)
        F = self.frame
        min_w, min_h = F.width() * 0.04, F.height() * 0.04
        if self.mode[0] == "move":
            r.translate(d)
            r.moveLeft(min(max(F.left(), r.left()), F.right() - r.width()))
            r.moveTop(min(max(F.top(), r.top()), F.bottom() - r.height()))
            r = self._snap_move(r)
        else:
            c = self.mode[1]
            if c in (0, 2):
                r.setLeft(min(max(F.left(), r.left() + d.x()), r.right() - min_w))
            else:
                r.setRight(max(min(F.right(), r.right() + d.x()), r.left() + min_w))
            if c in (0, 1):
                r.setTop(min(max(F.top(), r.top() + d.y()), r.bottom() - min_h))
            else:
                r.setBottom(max(min(F.bottom(), r.bottom() + d.y()), r.top() + min_h))
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier and self.start_rect.height() > 0:
                ratio = self.start_rect.width() / self.start_rect.height()
                if c in (0, 1):
                    r.setTop(r.bottom() - r.width() / ratio)
                else:
                    r.setBottom(r.top() + r.width() / ratio)
        self.set_rect(r)

    def _snap_move(self, r: QRectF):
        """Gentle snapping to the frame's safe margins and centre lines."""
        F = self.frame
        tol = F.width() * 0.008
        mx, my = F.width() * 0.025, F.height() * 0.035
        for target, cur in ((F.left() + mx, r.left()), (F.right() - mx, r.right()), (F.center().x(), r.center().x())):
            if abs(target - cur) < tol:
                r.translate(target - cur, 0)
                break
        for target, cur in ((F.top() + my, r.top()), (F.bottom() - my, r.bottom()), (F.center().y(), r.center().y())):
            if abs(target - cur) < tol:
                r.translate(0, target - cur)
                break
        return r

    def mouseReleaseEvent(self, e):
        if self.mode:
            self.mode = None
            self.update()
            self.committed.emit()


class PreviewView(QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumSize(480, 300)
        self.restyle()

    def restyle(self):
        self.setBackgroundBrush(QColor(T["canvas"]))
        self.setStyleSheet("QGraphicsView { background: %s; border-radius: 14px; }" % T["canvas"])

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit()

    def fit(self):
        r = self.scene().sceneRect()
        if r.width() > 0:
            self.fitInView(r.adjusted(-16, -34, 16, 16), Qt.AspectRatioMode.KeepAspectRatio)


class _Signals(QObject):
    loaded = Signal(object)


class LoadPreviewJob(QRunnable):
    """Load a preview video's GPS track, one frame and its map tiles off the UI thread."""

    def __init__(self, path, cfg):
        super().__init__()
        self.path, self.cfg = path, cfg
        self.setAutoDelete(False)
        self.signals = _Signals()

    def run(self):
        try:
            track = load_track(self.path)
            W, H, dur = video_info(self.path)
            t = min(track.duration * 0.5, max(0.0, dur - 0.05))
            bg = grab_frame(self.path, t)
            Overlay(track, self.cfg, W, H).prefetch()
            self.signals.loaded.emit(dict(path=self.path, track=track, bg=bg, W=W, H=H, dur=dur, t=t))
        except Exception as e:
            self.signals.loaded.emit(dict(path=self.path, error=str(e)))


class FrameJob(QRunnable):
    def __init__(self, path, t, token):
        super().__init__()
        self.path, self.t, self.token = path, t, token
        self.setAutoDelete(False)
        self.signals = _Signals()

    def run(self):
        try:
            self.signals.loaded.emit(dict(token=self.token, bg=grab_frame(self.path, self.t), t=self.t))
        except Exception:
            pass


class Swatch(QAbstractButton):
    def __init__(self, color, parent=None):
        super().__init__(parent)
        self.color = color
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(26, 26)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isChecked():
            p.setPen(QPen(QColor(T["text"]), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(1, 1, 24, 24))
        p.setPen(Qt.PenStyle.NoPen)
        if self.color == "custom":
            g = QColor(T["raised"])
            p.setBrush(g)
            p.drawEllipse(QRectF(5, 5, 16, 16))
            p.setPen(QPen(QColor(T["muted"]), 1.6))
            p.drawLine(QPointF(13, 9), QPointF(13, 17))
            p.drawLine(QPointF(9, 13), QPointF(17, 13))
        else:
            p.setBrush(QColor(self.color))
            p.drawEllipse(QRectF(5, 5, 16, 16))


# ====================================================================== settings dialog
class Section(QFrame):
    """A titled card with optional on/off switch in its header."""

    def __init__(self, title, toggle=None, on_toggle=None):
        super().__init__()
        self.setProperty("role", "card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(label(title.upper(), "section"))
        head.addStretch(1)
        self.body = QWidget()
        self.form = QVBoxLayout(self.body)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setSpacing(10)
        if toggle is not None:
            sw = Toggle(toggle)
            sw.toggled.connect(self.body.setEnabled)
            if on_toggle:
                sw.toggled.connect(on_toggle)
            self.body.setEnabled(toggle)
            head.addWidget(sw)
        lay.addLayout(head)
        lay.addWidget(self.body)

    def row(self, text, widget, stretch_label=True):
        r = QHBoxLayout()
        r.setSpacing(12)
        l = QLabel(text)
        r.addWidget(l, 1 if stretch_label else 0)
        r.addWidget(widget, 0 if stretch_label else 1)
        self.form.addLayout(r)
        return widget

    def wide(self, text, widget):
        self.form.addWidget(label(text, "muted"))
        self.form.addWidget(widget)
        return widget


class SettingsDialog(QDialog):
    PREVIEW_W = 1280

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(1240, 760)
        self.cfg = settings_mod.load()
        self.pool = QThreadPool.globalInstance()
        self.track = demo_track()
        self.W, self.H = 3840, 2160
        self.duration = self.track.duration
        self.t = self.track.duration * 0.6
        self.offline = True
        self.bg_img = demo_background(self.W, self.H)
        self.preview_path = None
        self.frame_token = 0
        self._jobs = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(14)
        main = QHBoxLayout()
        main.setSpacing(16)
        outer.addLayout(main, 1)

        # ---------- left: preview
        left = QVBoxLayout()
        left.setSpacing(10)
        ph = QHBoxLayout()
        ph.addWidget(label("Layout", "wordmark"))
        ph.addSpacing(6)
        self.preview_label = label("Demo flight", "chip")
        ph.addWidget(self.preview_label)
        ph.addStretch(1)
        ph.addWidget(button("Preview with a video…", None, self.pick_preview_video))
        left.addLayout(ph)
        self.scene = QGraphicsScene()
        self.view = PreviewView(self.scene)
        self.bg_item = QGraphicsPixmapItem()
        self.bg_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene.addItem(self.bg_item)
        self.boxes = {}
        left.addWidget(self.view, 1)
        trow = QHBoxLayout()
        trow.setSpacing(12)
        self.time_label = label("0:00", "muted")
        self.time_label.setMinimumWidth(36)
        trow.addWidget(self.time_label)
        self.time_slider = QSlider(Qt.Orientation.Horizontal)
        self.time_slider.setRange(0, 1000)
        self.time_slider.setValue(600)
        self.time_slider.valueChanged.connect(self.on_time)
        trow.addWidget(self.time_slider, 1)
        left.addLayout(trow)
        left.addWidget(label("Drag the map or stats to move them · drag a corner to resize "
                             "(Shift keeps the shape)", "faint"))
        main.addLayout(left, 1)

        # ---------- right: options
        panel = QWidget()
        form = QVBoxLayout(panel)
        form.setContentsMargins(0, 0, 8, 0)
        form.setSpacing(12)
        form.addWidget(self._map_section())
        form.addWidget(self._stats_section())
        form.addWidget(self._look_section())
        form.addWidget(self._output_section())
        form.addWidget(self._app_section())
        form.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(panel)
        scroll.setFixedWidth(390)
        main.addWidget(scroll)

        btns = QHBoxLayout()
        btns.addWidget(button("Reset layout", "ghost", self.reset_layout))
        btns.addWidget(button("Restore defaults", "ghost", self.restore_defaults))
        btns.addStretch(1)
        btns.addWidget(button("Cancel", None, self.reject))
        save = button("Save", "primary", self.save)
        save.setDefault(True)
        btns.addWidget(save)
        outer.addLayout(btns)

        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(120)
        self.render_timer.timeout.connect(self.refresh_widgets)

        self.setup_scene()
        last = self.cfg["app"].get("last_video")
        if last and os.path.exists(last):
            self.load_preview(last)

    # ----- controls bound to settings
    def _toggle(self, section, key):
        t = Toggle(bool(self.cfg[section].get(key)))
        t.toggled.connect(lambda v: self._set(section, key, v))
        return t

    def _seg(self, section, key, items):
        s = Segmented(items, self.cfg[section].get(key))
        s.changed.connect(lambda v: self._set(section, key, v))
        return s

    def _slider(self, section, key, lo, hi, scale=1.0):
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setValue(int(round(float(self.cfg[section][key]) * scale)))
        s.valueChanged.connect(lambda v: self._set(section, key, v / scale if scale != 1 else v))
        s.setMinimumWidth(170)
        return s

    def _map_section(self):
        s = Section("Map", self.cfg["map"]["enabled"], lambda v: self._set("map", "enabled", v))
        z = self._slider("map", "zoom", 13, 19)
        zr = QWidget()
        zl = QHBoxLayout(zr)
        zl.setContentsMargins(0, 0, 0, 0)
        zl.addWidget(label("Far", "faint"))
        zl.addWidget(z, 1)
        zl.addWidget(label("Close", "faint"))
        s.wide("Zoom", zr)
        s.wide("Orientation", self._seg("map", "orientation", [("North up", "north"), ("Flight direction up", "heading")]))
        s.wide("Map style", self._seg("map", "style", [("Standard", "standard"), ("Grayscale", "grayscale"), ("Dark", "dark")]))
        s.row("Path flown", self._toggle("map", "show_path"))
        s.row("Whole route ahead", self._toggle("map", "show_full_route"))
        s.row("Camera direction", self._toggle("map", "show_cone"))
        s.row("North marker", self._toggle("map", "show_north"))
        s.row("Map credit (© OpenStreetMap)", self._toggle("map", "show_credit"))
        return s

    def _stats_section(self):
        s = Section("Flight stats", self.cfg["stats"]["enabled"], lambda v: self._set("stats", "enabled", v))
        s.row("Speed", self._toggle("stats", "speed"))
        s.row("Altitude", self._toggle("stats", "altitude"))
        s.row("Heading", self._toggle("stats", "heading"))
        s.row("Distance flown", self._toggle("stats", "distance"))
        s.row("Coordinates", self._toggle("stats", "coords"))
        return s

    def _look_section(self):
        s = Section("Look")
        s.wide("Units", self._seg("style", "units", [("Metric  km/h · m", "metric"), ("Imperial  mph · ft", "imperial")]))
        sw = QWidget()
        sl = QHBoxLayout(sw)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        self.swatches = []
        for c in SWATCHES + ["custom"]:
            b = Swatch(c)
            b.clicked.connect(lambda _=False, c=c: self.pick_accent(c))
            self.swatches.append(b)
            sl.addWidget(b)
        sl.addStretch(1)
        self._mark_swatch()
        s.wide("Accent colour", sw)
        s.row("Stats background", self._slider("style", "panel_opacity", 0, 100, 100.0))
        s.row("Corner roundness", self._slider("style", "roundness", 0, 25, 100.0))
        return s

    def _output_section(self):
        s = Section("Output")
        s.wide("Format", self._seg("output", "codec", [("Same as original", "auto"), ("H.264 · plays everywhere", "h264")]))
        fr = QWidget()
        fl = QHBoxLayout(fr)
        fl.setContentsMargins(0, 0, 0, 0)
        self.folder_label = label("", "muted")
        self.folder_label.setWordWrap(True)
        fl.addWidget(self.folder_label, 1)
        fl.addWidget(button("Choose…", None, self.pick_folder))
        self.folder_reset = button("Reset", "ghost", self.reset_folder)
        fl.addWidget(self.folder_reset)
        self._paint_folder()
        s.wide("Save to", fr)
        suf = QLineEdit(self.cfg["output"]["suffix"])
        suf.setFixedWidth(120)
        suf.textChanged.connect(lambda v: self._set("output", "suffix", v or "_map", rerender=False))
        s.row("File name ending", suf)
        return s

    def _app_section(self):
        s = Section("Appearance")
        seg = Segmented([("Dark", "dark"), ("Light", "light"), ("System", "system")], self.cfg["app"].get("theme", "dark"))
        seg.changed.connect(self.set_theme)
        s.wide("Theme", seg)
        return s

    def _mark_swatch(self):
        cur = self.cfg["style"]["accent"].lower()
        for b in self.swatches:
            b.setChecked(b.color == cur or (b.color == "custom" and cur not in SWATCHES))
            b.update()

    def pick_accent(self, c):
        if c == "custom":
            q = QColorDialog.getColor(QColor(self.cfg["style"]["accent"]), self, "Accent colour")
            if not q.isValid():
                self._mark_swatch()
                return
            c = q.name()
        self._set("style", "accent", c)
        self._mark_swatch()

    def _paint_folder(self):
        f = self.cfg["output"]["folder"]
        self.folder_label.setText(f if f else "Next to the original video")
        self.folder_reset.setVisible(bool(f))

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Save videos to",
                                             self.cfg["output"]["folder"] or os.path.expanduser("~"))
        if d:
            self._set("output", "folder", d, rerender=False)
            self._paint_folder()

    def reset_folder(self):
        self._set("output", "folder", "", rerender=False)
        self._paint_folder()

    def set_theme(self, mode):
        self.cfg["app"]["theme"] = mode
        theme.apply(QApplication.instance(), mode)
        self.view.restyle()
        self.update()

    def _set(self, section, key, value, rerender=True):
        self.cfg[section][key] = value
        if key == "enabled":
            self.update_box_visibility()
        if rerender:
            self.render_timer.start()

    # ----- preview scene
    def setup_scene(self):
        PW = self.PREVIEW_W
        PH = PW * self.H / self.W
        self.PW, self.PH = PW, PH
        self.scene.setSceneRect(QRectF(0, 0, PW, PH))
        self._set_bg(self.bg_img)
        frame = QRectF(0, 0, PW, PH)
        for name, title in (("map", "Map"), ("stats", "Stats")):
            if name not in self.boxes:
                b = BoxItem(name, title, frame)
                b.committed.connect(self.on_box_committed)
                self.scene.addItem(b)
                self.boxes[name] = b
            b = self.boxes[name]
            b.frame = frame
            x, y, w, h = self.cfg[name]["box"]
            b.set_rect(QRectF(x * PW, y * PH, w * PW, h * PH))
        self.update_box_visibility()
        self.view.fit()
        self.time_label.setText(fmt_secs(self.t))
        self.refresh_widgets()

    def _set_bg(self, img):
        pm = pil_to_pixmap(img.resize((int(self.PW), int(self.PH)), Image.BILINEAR))
        rounded = QPixmap(pm.size())
        rounded.fill(Qt.GlobalColor.transparent)
        p = QPainter(rounded)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rounded.rect()), 10, 10)
        p.setClipPath(path)
        p.drawPixmap(0, 0, pm)
        p.end()
        self.bg_item.setPixmap(rounded)

    def update_box_visibility(self):
        for name, b in self.boxes.items():
            b.setVisible(bool(self.cfg[name]["enabled"]))

    def on_box_committed(self):
        for name, b in self.boxes.items():
            r = b.rect
            self.cfg[name]["box"] = [round(r.x() / self.PW, 4), round(r.y() / self.PH, 4),
                                     round(r.width() / self.PW, 4), round(r.height() / self.PH, 4)]
        self.refresh_widgets()

    def refresh_widgets(self):
        try:
            ov = Overlay(self.track, self.cfg, self.W, self.H, offline=self.offline)
        except Exception as e:
            self.preview_label.setText("Preview error: %s" % e)
            return
        k = self.PW / self.W
        for b in self.boxes.values():
            b.pix = None
        for name, w, _, _ in ov.widgets:
            img = w.render(self.t)
            disp = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.LANCZOS)
            self.boxes[name].set_pixmap(pil_to_pixmap(disp), w.margin * k)
        for b in self.boxes.values():
            b.update()

    def on_time(self, v):
        self.t = self.duration * v / 1000.0
        self.time_label.setText(fmt_secs(self.t))
        self.render_timer.start()
        if self.preview_path:
            self.frame_token += 1
            job = FrameJob(self.preview_path, self.t, self.frame_token)
            job.signals.loaded.connect(self.on_frame)
            self._jobs = self._jobs[-8:] + [job]
            QTimer.singleShot(150, lambda tok=self.frame_token, j=job: self._start_if_current(tok, j))

    def _start_if_current(self, token, job):
        if token == self.frame_token:
            self.pool.start(job)

    def on_frame(self, d):
        if d.get("token") == self.frame_token:
            self.bg_img = d["bg"]
            self._set_bg(self.bg_img)

    def pick_preview_video(self):
        f, _ = QFileDialog.getOpenFileName(self, "Preview with video", os.path.dirname(
            self.cfg["app"].get("last_video") or os.path.expanduser("~")),
            "Videos (*.mp4 *.MP4 *.mov *.MOV *.m4v *.mkv);;All files (*)")
        if f:
            self.load_preview(f)

    def load_preview(self, path):
        self.preview_label.setText("Loading %s…" % os.path.basename(path))
        job = LoadPreviewJob(path, copy.deepcopy(self.cfg))
        job.signals.loaded.connect(self.on_preview_loaded)
        self._jobs.append(job)
        self.pool.start(job)

    def on_preview_loaded(self, d):
        if "error" in d:
            self.preview_label.setText("Couldn't use %s" % os.path.basename(d["path"]))
            self.preview_label.setToolTip(d["error"])
            return
        self.preview_path = d["path"]
        self.track, self.W, self.H, self.duration, self.t = d["track"], d["W"], d["H"], d["dur"], d["t"]
        self.offline = False
        self.bg_img = d["bg"]
        self.preview_label.setText(os.path.basename(d["path"]))
        self.time_slider.blockSignals(True)
        self.time_slider.setValue(int(1000 * self.t / max(0.001, self.duration)))
        self.time_slider.blockSignals(False)
        self.setup_scene()

    def reset_layout(self):
        d = settings_mod.defaults()
        self.cfg["map"]["box"] = d["map"]["box"]
        self.cfg["stats"]["box"] = d["stats"]["box"]
        self.setup_scene()

    def restore_defaults(self):
        if QMessageBox.question(self, "Restore defaults", "Reset every setting to its default?") \
                != QMessageBox.StandardButton.Yes:
            return
        keep = self.cfg["app"]
        self.cfg = settings_mod.defaults()
        self.cfg["app"] = keep
        settings_mod.save(self.cfg)
        self.accept()
        SettingsDialog(self.parent()).exec()

    def reject(self):
        # undo a live theme preview if it wasn't saved
        theme.apply(QApplication.instance(), settings_mod.load()["app"].get("theme", "dark"))
        super().reject()

    def save(self):
        self.on_box_committed()
        settings_mod.save(self.cfg)
        self.accept()


# ====================================================================== app
class App(QApplication):
    file_opened = Signal(str)

    def event(self, e):
        # macOS: files dropped on the Dock icon / "Open With"
        if e.type() == QEvent.Type.FileOpen:
            self.file_opened.emit(e.file())
            return True
        return super().event(e)


def run(files=None):
    app = App.instance() or App(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("DroneMapOverlay")
    theme.apply(app, settings_mod.load()["app"].get("theme", "dark"))
    ic = asset("icon.png")
    if ic:
        app.setWindowIcon(QIcon(ic))
    w = MainWindow()
    app.file_opened.connect(lambda p: w.add_files([p]))
    w.show()
    files = [f for f in (files or []) if os.path.isfile(f)]
    if files:
        QTimer.singleShot(200, lambda: w.add_files(files))
    return app.exec()
