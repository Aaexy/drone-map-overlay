"""Look and feel: colour tokens, the app stylesheet, and a few custom-painted controls."""
from __future__ import annotations

import math

from PySide6.QtCore import Property, QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup, QHBoxLayout, QPushButton,
                               QStyleFactory, QWidget)

ACCENT = "#ff5c36"

DARK = dict(
    bg="#0c0f14", surface="#141920", surface2="#1b212a", raised="#222935", border="#262e3a",
    border_hi="#343e4d", text="#e9edf3", muted="#8b95a7", faint="#5a6476", accent=ACCENT,
    accent_hi="#ff7756", accent_soft="rgba(255,92,54,0.14)", success="#3ddc97", track="#2a323f",
    grid="#1a2029", canvas="#07090c",
)
LIGHT = dict(
    bg="#f3f4f6", surface="#ffffff", surface2="#f0f2f5", raised="#e8ebf0", border="#dfe3e9",
    border_hi="#c9d0da", text="#11151b", muted="#5c6676", faint="#9aa3b1", accent=ACCENT,
    accent_hi="#ff7756", accent_soft="rgba(255,92,54,0.12)", success="#1fae6f", track="#d9dee6",
    grid="#e9ecf0", canvas="#dfe3e8",
)

T = dict(DARK)  # current tokens; widgets read from here when painting


def resolve(mode: str) -> str:
    if mode in ("dark", "light"):
        return mode
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
        return "light" if scheme == Qt.ColorScheme.Light else "dark"
    except Exception:
        return "dark"


def apply(app: QApplication, mode: str = "dark"):
    m = resolve(mode)
    T.clear()
    T.update(DARK if m == "dark" else LIGHT)
    app.setStyle(QStyleFactory.create("Fusion"))
    try:  # dark/light native title bars (Qt 6.8+)
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark if m == "dark" else Qt.ColorScheme.Light)
    except Exception:
        pass
    pal = QPalette()
    c = lambda k: QColor(T[k]) if not T[k].startswith("rgba") else QColor(T["raised"])
    pal.setColor(QPalette.ColorRole.Window, c("bg"))
    pal.setColor(QPalette.ColorRole.WindowText, c("text"))
    pal.setColor(QPalette.ColorRole.Base, c("surface2"))
    pal.setColor(QPalette.ColorRole.AlternateBase, c("surface"))
    pal.setColor(QPalette.ColorRole.Text, c("text"))
    pal.setColor(QPalette.ColorRole.Button, c("surface2"))
    pal.setColor(QPalette.ColorRole.ButtonText, c("text"))
    pal.setColor(QPalette.ColorRole.Highlight, c("accent"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, c("raised"))
    pal.setColor(QPalette.ColorRole.ToolTipText, c("text"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, c("faint"))
    pal.setColor(QPalette.ColorRole.Mid, c("border"))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet())
    for w in app.allWidgets():
        w.update()
    return m


def stylesheet() -> str:
    return """
* { outline: none; }
QWidget { color: %(text)s; font-size: 13px; }
QMainWindow, QDialog { background: %(bg)s; }
QLabel { background: transparent; }
QLabel[role="muted"] { color: %(muted)s; }
QLabel[role="faint"] { color: %(faint)s; font-size: 12px; }
QLabel[role="h1"] { font-size: 22px; font-weight: 700; }
QLabel[role="wordmark"] { font-size: 15px; font-weight: 700; letter-spacing: 0.2px; }
QLabel[role="chip"] { color: %(muted)s; background: %(surface2)s; border: 1px solid %(border)s;
    border-radius: 8px; padding: 1px 7px; font-size: 11px; }
QLabel[role="section"] { color: %(muted)s; font-size: 11px; font-weight: 700; letter-spacing: 1.4px; }
QLabel[role="pct"] { color: %(text)s; font-weight: 700; font-size: 13px; }

QFrame[role="card"] { background: %(surface)s; border: 1px solid %(border)s; border-radius: 14px; }

QPushButton { background: %(surface2)s; border: 1px solid %(border)s; border-radius: 9px;
    padding: 7px 14px; color: %(text)s; font-weight: 600; }
QPushButton:hover { background: %(raised)s; border-color: %(border_hi)s; }
QPushButton:pressed { background: %(border)s; }
QPushButton:disabled { color: %(faint)s; }
QPushButton[kind="primary"] { background: %(accent)s; border: 1px solid %(accent)s; color: white; padding: 9px 20px; }
QPushButton[kind="primary"]:hover { background: %(accent_hi)s; border-color: %(accent_hi)s; }
QPushButton[kind="ghost"] { background: transparent; border: 1px solid transparent; color: %(muted)s; }
QPushButton[kind="ghost"]:hover { background: %(surface2)s; color: %(text)s; }
QPushButton[kind="seg"] { background: transparent; border: none; border-radius: 7px; padding: 6px 10px;
    color: %(muted)s; font-weight: 600; }
QPushButton[kind="seg"]:hover { color: %(text)s; }
QPushButton[kind="seg"]:checked { background: %(raised)s; color: %(text)s; }
QWidget[role="segbar"] { background: %(surface2)s; border: 1px solid %(border)s; border-radius: 9px; }

QLineEdit { background: %(surface2)s; border: 1px solid %(border)s; border-radius: 8px; padding: 6px 9px;
    selection-background-color: %(accent)s; }
QLineEdit:focus { border-color: %(accent)s; }

QSlider::groove:horizontal { height: 4px; background: %(track)s; border-radius: 2px; }
QSlider::sub-page:horizontal { background: %(accent)s; border-radius: 2px; }
QSlider::handle:horizontal { background: white; width: 16px; height: 16px; margin: -6px 0;
    border-radius: 8px; border: 1px solid rgba(0,0,0,0.25); }
QSlider::handle:horizontal:hover { background: #fff; border: 2px solid %(accent)s; }

QProgressBar { background: %(track)s; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }
QProgressBar::chunk { background: %(accent)s; border-radius: 3px; }

QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: %(border_hi)s; border-radius: 3px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QToolTip { background: %(raised)s; color: %(text)s; border: 1px solid %(border_hi)s; border-radius: 6px; padding: 4px 8px; }
QMessageBox { background: %(surface)s; }
QMenu { background: %(surface)s; border: 1px solid %(border)s; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 14px; border-radius: 5px; }
QMenu::item:selected { background: %(accent_soft)s; }
""" % T


# ------------------------------------------------------------------ controls
class Toggle(QAbstractButton):
    """iOS-style switch."""

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def sizeHint(self):
        return QSize(38, 22)

    def _animate(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def get_knob(self):
        return self._pos

    def set_knob(self, v):
        self._pos = v
        self.update()

    knob = Property(float, get_knob, set_knob)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(1, 2, 36, 20)
        off, on = QColor(T["track"]), QColor(T["accent"])
        mix = QColor(int(off.red() + (on.red() - off.red()) * self._pos),
                     int(off.green() + (on.green() - off.green()) * self._pos),
                     int(off.blue() + (on.blue() - off.blue()) * self._pos))
        if not self.isEnabled():
            mix.setAlpha(110)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mix)
        p.drawRoundedRect(r, 10, 10)
        x = r.left() + 2 + (r.width() - 20) * self._pos
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawEllipse(QRectF(x, r.top() + 2.6, 16, 16))
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(x, r.top() + 2, 16, 16))


class Segmented(QWidget):
    """A row of mutually exclusive buttons (instead of a dropdown)."""
    changed = Signal(object)

    def __init__(self, items, value=None, parent=None):
        super().__init__(parent)
        self.setProperty("role", "segbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.values = []
        for i, (label, val) in enumerate(items):
            b = QPushButton(label)
            b.setProperty("kind", "seg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if val == value:
                b.setChecked(True)
            self.group.addButton(b, i)
            self.values.append(val)
            lay.addWidget(b, 1)
        if self.group.checkedId() < 0 and self.values:
            self.group.button(0).setChecked(True)
        self.group.idClicked.connect(lambda i: self.changed.emit(self.values[i]))


class MapPattern(QWidget):
    """Faint street-grid + flight-path artwork painted behind the drop zone."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 18, 18)
        p.setClipPath(path)
        p.fillPath(path, QColor(T["surface"]))
        grid = QColor(T["grid"])
        p.setPen(QPen(grid, 6))
        step = 90
        for i in range(-6, int(w / step) + 8):
            x = i * step
            p.drawLine(QPointF(x, 0), QPointF(x + h * 0.6, h))
        p.setPen(QPen(grid, 3))
        for i in range(-4, int(h / 70) + 6):
            y = i * 70
            p.drawLine(QPointF(0, y + w * 0.12), QPointF(w, y - w * 0.12))
        # a dashed flight path sweeping across the card
        acc = QColor(T["accent"])
        acc.setAlpha(150 if self.hover else 70)
        pen = QPen(acc, 3)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([2, 3])
        p.setPen(pen)
        fp = QPainterPath(QPointF(w * 0.06, h * 0.86))
        fp.cubicTo(QPointF(w * 0.3, h * 0.55), QPointF(w * 0.62, h * 1.0), QPointF(w * 0.94, h * 0.2))
        p.drawPath(fp)
        # border
        border = QColor(T["accent"]) if self.hover else QColor(T["border"])
        pen = QPen(border, 1.5)
        if self.hover:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setClipping(False)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(0.75, 0.75, w - 1.5, h - 1.5), 18, 18)
