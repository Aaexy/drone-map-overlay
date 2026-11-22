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
    QInputDialog,
    QLineEdit,
    QMenu,
    QMessageBox,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .. import settings as settings_mod
from ..layout import box_to_px, px_to_box
from ..pipeline import demo_background, demo_track
from . import theme
from .canvas import BoxItem, PreviewView
from .controls import Section, Segmented, Swatch, Toggle, button, label
from .platform import open_file
from .workers import FrameJob, PreviewLoadJob, WidgetPreviewJob

log = logging.getLogger(__name__)

SWATCHES = ["#ff5c36", "#ffb020", "#3ddc97", "#22c3ff", "#8b7bff", "#ff4d8d"]
PRESET_FILTER = "Drone Map Overlay presets (*.json);;All files (*)"
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
        self.preview_token = 0
        self.shown_token = 0
        self.sections = {}
        self._jobs = set()  # running background jobs, held until they report back

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
        reset = button("↺  Reset positions", None, self.reset_layout)
        reset.setToolTip("Put the map and stats back where they start (right-click a box to reset just that one)")
        ph.addWidget(reset)
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
        self.busy = label("Updating map…", "faint")
        self.busy.setVisible(False)
        trow.addWidget(self.busy)
        self.map_warning = label("⚠  Map couldn't be downloaded", "muted")
        self.map_warning.setStyleSheet("color: %s;" % theme.T["accent"])
        self.map_warning.setVisible(False)
        trow.addWidget(self.map_warning)
        left.addLayout(trow)
        left.addWidget(
            label(
                "Drag the map or stats to move them · drag a corner to resize (Shift keeps the shape) · right-click for more",
                "faint",
            )
        )
        main.addLayout(left, 1)

        # ---------- right: presets + options
        right = QVBoxLayout()
        right.setSpacing(10)
        prow = QHBoxLayout()
        prow.setSpacing(8)
        self.preset_btn = button("", None, None)
        self.preset_btn.setToolTip("Saved looks and layouts: apply one, or save the current settings as a preset")
        self.preset_btn.setStyleSheet("text-align: left;")
        self.preset_btn.clicked.connect(self.preset_menu)
        prow.addWidget(self.preset_btn, 1)
        save_preset = button("Save as preset…", None, self.save_preset_as)
        save_preset.setToolTip("Save the map, stats and look (not the output folder) as a preset you can reuse")
        prow.addWidget(save_preset)
        right.addLayout(prow)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(self._options_panel())
        right.addWidget(self.scroll, 1)
        rw = QWidget()
        rw.setLayout(right)
        rw.setFixedWidth(390)
        right.setContentsMargins(0, 0, 0, 0)
        main.addWidget(rw)
        self._paint_preset()

        btns = QHBoxLayout()
        btns.addWidget(button("Restore defaults", "ghost", self.restore_defaults))
        btns.addStretch(1)
        btns.addWidget(button("Cancel", None, self.reject))
        btns.addWidget(button("Save", None, self.save))
        render = button("Save && render  ▶", "primary", self.save_and_render)
        render.setToolTip("Save these settings and render the preview video (or choose videos)")
        render.setDefault(True)
        btns.addWidget(render)
        outer.addLayout(btns)

        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(120)
        self.render_timer.timeout.connect(self.refresh_widgets)

        self.setup_scene()
        last = self.cfg["app"].get("last_video")
        if last and os.path.exists(last):
            self.load_preview(last)

    def _options_panel(self):
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
        return panel

    def rebuild_options(self):
        """Recreate the option controls so they show self.cfg (after applying a preset)."""
        pos = self.scroll.verticalScrollBar().value()
        old = self.scroll.takeWidget()
        self.scroll.setWidget(self._options_panel())
        if old is not None:
            old.deleteLater()
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(pos))

    # ----- presets
    def _current_preset(self):
        """Name of the preset the current settings match, or None if they've been changed."""
        mine = settings_mod.preset_from(self.cfg, "")
        for p in settings_mod.list_presets():
            try:
                theirs = settings_mod.preset_from(settings_mod.apply_preset(self.cfg, settings_mod.load_preset(p["path"])), "")
            except settings_mod.PresetError:
                continue
            if all(theirs[k] == mine[k] for k in settings_mod.PRESET_SECTIONS):
                return p["name"]
        return None

    def _paint_preset(self):
        name = self._current_preset()
        self.preset_btn.setText("Preset:  %s   ▾" % (name or "Custom"))

    def preset_menu(self):
        m = QMenu(self)
        current = self._current_preset()
        presets = settings_mod.list_presets()
        for p in presets:
            a = m.addAction(p["name"], lambda p=p: self.apply_preset(p["path"]))
            a.setCheckable(True)
            a.setChecked(p["name"] == current)
        m.addSeparator()
        m.addAction("Save current settings as preset…", self.save_preset_as)
        mine = [p for p in presets if not p["builtin"]]
        if mine:
            ren = m.addMenu("Rename")
            dele = m.addMenu("Delete")
            for p in mine:
                ren.addAction(p["name"], lambda p=p: self.rename_preset(p["name"]))
                dele.addAction(p["name"], lambda p=p: self.delete_preset(p["name"]))
        m.addSeparator()
        m.addAction("Import preset…", self.import_preset)
        m.addAction("Export current settings…", self.export_preset)
        m.addAction("Open presets folder", self.open_presets_folder)
        m.exec(self.preset_btn.mapToGlobal(self.preset_btn.rect().bottomLeft()))

    def apply_preset(self, path):
        try:
            self.cfg = settings_mod.apply_preset(self.cfg, settings_mod.load_preset(path))
        except settings_mod.PresetError as e:
            QMessageBox.warning(self, "Preset", str(e))
            return
        self.rebuild_options()
        self.setup_scene()
        self._paint_preset()

    def _ask_name(self, title, text, default=""):
        name, ok = QInputDialog.getText(self, title, text, QLineEdit.EchoMode.Normal, default)
        return name.strip() if ok and name.strip() else None

    def save_preset_as(self):
        self.on_box_committed()
        name = self._ask_name("Save preset", "Name for this look and layout:", self._current_preset() or "")
        if not name:
            return
        existing = settings_mod.find_preset(name)
        if existing and not os.path.isfile(name):
            what = "a built-in preset (yours will be used instead)" if existing["builtin"] else "your preset"
            if (
                QMessageBox.question(self, "Save preset", "Replace %s “%s”?" % (what, existing["name"]))
                != QMessageBox.StandardButton.Yes
            ):
                return
        try:
            settings_mod.save_preset(name, self.cfg)
        except (OSError, settings_mod.PresetError) as e:
            QMessageBox.warning(self, "Save preset", "Couldn't save the preset: %s" % e)
        self._paint_preset()

    def rename_preset(self, old):
        new = self._ask_name("Rename preset", "New name:", old)
        if not new or new == old:
            return
        try:
            settings_mod.rename_preset(old, new)
        except (OSError, settings_mod.PresetError) as e:
            QMessageBox.warning(self, "Rename preset", str(e))
        self._paint_preset()

    def delete_preset(self, name):
        if QMessageBox.question(self, "Delete preset", "Delete the preset “%s”?" % name) != QMessageBox.StandardButton.Yes:
            return
        try:
            settings_mod.delete_preset(name)
        except (OSError, settings_mod.PresetError) as e:
            QMessageBox.warning(self, "Delete preset", str(e))
        self._paint_preset()

    def import_preset(self):
        f, _ = QFileDialog.getOpenFileName(self, "Import preset", os.path.expanduser("~"), PRESET_FILTER)
        if not f:
            return
        try:
            name = settings_mod.import_preset(f)
        except (OSError, settings_mod.PresetError) as e:
            QMessageBox.warning(self, "Import preset", str(e))
            return
        self.apply_preset(name)

    def export_preset(self):
        self.on_box_committed()
        name = self._current_preset() or "My layout"
        f, _ = QFileDialog.getSaveFileName(
            self, "Export preset", os.path.join(os.path.expanduser("~"), settings_mod._slug(name) + ".json"), PRESET_FILTER
        )
        if not f:
            return
        try:
            settings_mod.export_preset(self.cfg, name, f)
        except OSError as e:
            QMessageBox.warning(self, "Export preset", "Couldn't save the preset: %s" % e)

    def open_presets_folder(self):
        os.makedirs(settings_mod.presets_dir(), exist_ok=True)
        open_file(settings_mod.presets_dir())

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
        s = self.sections["map"] = Section("Map", self.cfg["map"]["enabled"], lambda v: self._set("map", "enabled", v))
        z = self._slider("map", "zoom", 13, 19)
        zr = QWidget()
        zl = QHBoxLayout(zr)
        zl.setContentsMargins(0, 0, 0, 0)
        zl.addWidget(label("Far", "faint"))
        zl.addWidget(z, 1)
        zl.addWidget(label("Close", "faint"))
        s.wide("Zoom", zr)
        s.wide("Orientation", self._seg("map", "orientation", [("North up", "north"), ("Flight direction up", "heading")]))
        s.wide(
            "Map style",
            self._seg("map", "style", [("Light", "light"), ("Dark", "dark"), ("Color", "color"), ("Satellite", "satellite")]),
        )
        s.wide("Shape", self._seg("map", "shape", [("Rounded", "rounded"), ("Circle", "circle")]))
        trail = self._seg("map", "trail", [("Whole flight", "full"), ("Recent", "recent"), ("Off", "off")])
        s.wide("Trail", trail)
        secs = QSlider(Qt.Orientation.Horizontal)
        secs.setRange(5, 120)
        secs.setValue(int(self.cfg["map"]["trail_seconds"]))
        secs_label = label("%d s" % secs.value(), "muted")
        secs.valueChanged.connect(lambda v: (secs_label.setText("%d s" % v), self._set("map", "trail_seconds", v)))
        sr = QWidget()
        srl = QHBoxLayout(sr)
        srl.setContentsMargins(0, 0, 0, 0)
        srl.addWidget(label("Recent trail length", "muted"))
        srl.addWidget(secs, 1)
        srl.addWidget(secs_label)
        sr.setVisible(self.cfg["map"]["trail"] == "recent")
        trail.changed.connect(lambda v: sr.setVisible(v == "recent"))
        s.form.addWidget(sr)
        s.wide("Marker", self._seg("map", "marker", [("Arrow", "arrow"), ("Dot", "dot")]))
        s.row("Map opacity", self._slider("map", "opacity", 30, 100, 100.0))
        s.row("Whole route ahead", self._toggle("map", "show_full_route"))
        s.row("Camera direction", self._toggle("map", "show_cone"))
        s.row("North marker", self._toggle("map", "show_north"))
        s.row("Border", self._toggle("map", "show_border"))
        s.row("Map credit", self._toggle("map", "show_credit"))
        return s

    def _stats_section(self):
        s = self.sections["stats"] = Section(
            "Flight stats", self.cfg["stats"]["enabled"], lambda v: self._set("stats", "enabled", v)
        )
        s.wide("Layout", self._seg("stats", "layout", [("Side by side", "row"), ("Stacked", "column")]))
        s.wide("Panel", self._seg("stats", "theme", [("Dark", "dark"), ("Light", "light")]))
        s.row("Speed", self._toggle("stats", "speed"))
        s.row("Altitude", self._toggle("stats", "altitude"))
        s.row("Vertical speed", self._toggle("stats", "vspeed"))
        s.row("Heading", self._toggle("stats", "heading"))
        s.row("Distance flown", self._toggle("stats", "distance"))
        s.row("Distance from start", self._toggle("stats", "home"))
        s.row("Time", self._toggle("stats", "time"))
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
        s.row("Drop shadow", self._toggle("style", "shadow"))
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
        if section in settings_mod.PRESET_SECTIONS and hasattr(self, "preset_btn"):
            self._paint_preset()

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
                b.menu_requested.connect(self.box_menu)
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
        self._paint_preset()
        self.refresh_widgets()

    def refresh_widgets(self):
        """Re-render the widgets in the background; stale results are dropped."""
        self.preview_token += 1
        job = WidgetPreviewJob(
            self.preview_token, self.track, copy.deepcopy(self.cfg), self.W, self.H, self.t, self.offline, self.PW / self.W
        )
        self._run(job, self.on_widgets_rendered)
        # Only show the indicator if it takes a moment (e.g. downloading satellite imagery).
        QTimer.singleShot(300, lambda tok=self.preview_token: self.busy.setVisible(self.shown_token < tok))

    def on_widgets_rendered(self, d):
        if d.get("token") != self.preview_token:
            return
        self.shown_token = d["token"]
        self.busy.setVisible(False)
        if "error" in d:
            self.preview_label.setToolTip(d["error"])
            return
        err = d.get("tile_error")
        self.map_warning.setVisible(bool(err) and not self.offline)
        if err:
            self.map_warning.setToolTip(
                "Some map tiles couldn't be downloaded, so parts of the map are blank.\n"
                "Check your internet connection.\n\nDetails: %s" % err
            )
        shown = set()
        for name, img, margin in d["items"]:
            self.boxes[name].set_pixmap(pil_to_pixmap(img), margin)
            shown.add(name)
        for name, b in self.boxes.items():
            if name not in shown:
                b.pix = None
            b.update()

    def box_menu(self, name, pos):
        m = QMenu(self)
        title = "map" if name == "map" else "stats"
        m.addAction("Reset %s position" % title, lambda: self.reset_layout(only=name))
        m.addAction("Hide %s" % title, lambda: self.sections[name].switch.setChecked(False))
        m.exec(pos)

    def on_time(self, v):
        self.t = self.duration * v / 1000.0
        self.time_label.setText(fmt_secs(self.t))
        self.render_timer.start()
        if self.preview_path:
            self.frame_token += 1
            QTimer.singleShot(150, lambda tok=self.frame_token: self._load_frame(tok))

    def _load_frame(self, token):
        if token == self.frame_token:  # the slider has settled
            self._run(FrameJob(self.preview_path, self.t, token), self.on_frame)

    def _run(self, job, handler):
        """Start a background job, keeping it alive until it has reported back."""
        self._jobs.add(job)
        job.signals.done.connect(handler)
        job.signals.done.connect(lambda _result, j=job: self._jobs.discard(j))
        self.pool.start(job)

    def on_frame(self, d):
        if d.get("token") == self.frame_token and "bg" in d:
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
        self._run(PreviewLoadJob(path, copy.deepcopy(self.cfg)), self.on_preview_loaded)

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

    def reset_layout(self, only=None):
        d = settings_mod.defaults()
        for name in [only] if only else ["map", "stats"]:
            self.cfg[name]["box"] = copy.deepcopy(d[name]["box"])
        self._paint_preset()
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

    def save_and_render(self):
        """Save, close, and queue the video being previewed (or let the user pick some)."""
        video = self.preview_path
        self.save()
        main = self.parent()
        if main is None or not hasattr(main, "add_files"):
            return
        if video and os.path.exists(video):
            main.add_files([video])
        else:
            QTimer.singleShot(0, main.open_dialog)
