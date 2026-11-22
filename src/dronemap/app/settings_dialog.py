"""Settings window: options on the right, a live layout editor on the left."""

from __future__ import annotations

import copy
import logging
import os

from PIL import Image
from PySide6.QtCore import QRectF, Qt, QThreadPool, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .. import settings as settings_mod
from ..layout import box_to_px, px_to_box
from ..overlay import Overlay
from ..pipeline import demo_background, demo_track
from . import theme
from .canvas import BoxItem, PreviewView
from .controls import Section, Segmented, Swatch, Toggle, button, label
from .workers import FrameJob, PreviewLoadJob

log = logging.getLogger(__name__)

SWATCHES = ["#ff5c36", "#ffb020", "#3ddc97", "#22c3ff", "#8b7bff", "#ff4d8d"]
VIDEO_FILTER = "Videos (*.mp4 *.MP4 *.mov *.MOV *.m4v *.mkv);;All files (*)"


def pil_to_pixmap(img: Image.Image) -> QPixmap:
    img = img.convert("RGBA")
    q = QImage(img.tobytes("raw", "RGBA"), img.width, img.height, img.width * 4, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(q.copy())


def fmt_secs(s: float) -> str:
    s = int(max(0, s))
    return "%d:%02d" % (s // 60, s % 60)


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
        left.addWidget(label("Drag the map or stats to move them · drag a corner to resize (Shift keeps the shape)", "faint"))
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
        d = QFileDialog.getExistingDirectory(self, "Save videos to", self.cfg["output"]["folder"] or os.path.expanduser("~"))
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
        # Long side of the preview is PREVIEW_W scene px, so portrait videos fit too.
        f = self.PREVIEW_W / max(self.W, self.H)
        PW, PH = self.W * f, self.H * f
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
            # Work at video resolution, then scale, so rounding matches the real render.
            x, y, w, h = box_to_px(self.cfg[name]["box"], self.W, self.H)
            b.set_rect(QRectF(x * f, y * f, w * f, h * f))
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
            if b.rect.width() > 0:
                r = b.rect
                self.cfg[name]["box"] = px_to_box(r.x(), r.y(), r.width(), r.height(), self.PW, self.PH)
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
        for p in ov.widgets:
            img = p.widget.render(self.t)
            disp = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.LANCZOS)
            self.boxes[p.name].set_pixmap(pil_to_pixmap(disp), p.widget.margin * k)
        for b in self.boxes.values():
            b.update()

    def on_time(self, v):
        self.t = self.duration * v / 1000.0
        self.time_label.setText(fmt_secs(self.t))
        self.render_timer.start()
        if self.preview_path:
            self.frame_token += 1
            job = FrameJob(self.preview_path, self.t, self.frame_token)
            job.signals.done.connect(self.on_frame)
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
        f, _ = QFileDialog.getOpenFileName(
            self,
            "Preview with video",
            os.path.dirname(self.cfg["app"].get("last_video") or os.path.expanduser("~")),
            "Videos (*.mp4 *.MP4 *.mov *.MOV *.m4v *.mkv);;All files (*)",
        )
        if f:
            self.load_preview(f)

    def load_preview(self, path):
        self.preview_label.setText("Loading %s…" % os.path.basename(path))
        job = PreviewLoadJob(path, copy.deepcopy(self.cfg))
        job.signals.done.connect(self.on_preview_loaded)
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
        if (
            QMessageBox.question(self, "Restore defaults", "Reset every setting to its default?")
            != QMessageBox.StandardButton.Yes
        ):
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
