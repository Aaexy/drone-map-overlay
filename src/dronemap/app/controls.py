"""Custom-painted controls and small layout helpers used across the app."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Property, QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .theme import T


def label(text: str, role: Optional[str] = None, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if role:
        lbl.setProperty("role", role)
    lbl.setWordWrap(wrap)
    return lbl


def button(text: str, kind: Optional[str] = None, cb=None) -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if cb:
        b.clicked.connect(cb)
    return b


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
        mix = QColor(
            int(off.red() + (on.red() - off.red()) * self._pos),
            int(off.green() + (on.green() - off.green()) * self._pos),
            int(off.blue() + (on.blue() - off.blue()) * self._pos),
        )
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


class Swatch(QAbstractButton):
    """Round colour chip; "custom" shows a plus and opens a colour picker."""

    def __init__(self, color: str, parent=None):
        super().__init__(parent)
        self.color = color
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(26, 26)
        self.setToolTip("Custom colour" if color == "custom" else color)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isChecked():
            p.setPen(QPen(QColor(T["text"]), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(1, 1, 24, 24))
        p.setPen(Qt.PenStyle.NoPen)
        if self.color == "custom":
            p.setBrush(QColor(T["raised"]))
            p.drawEllipse(QRectF(5, 5, 16, 16))
            p.setPen(QPen(QColor(T["muted"]), 1.6))
            p.drawLine(QPointF(13, 9), QPointF(13, 17))
            p.drawLine(QPointF(9, 13), QPointF(17, 13))
        else:
            p.setBrush(QColor(self.color))
            p.drawEllipse(QRectF(5, 5, 16, 16))


class Section(QFrame):
    """A titled card, optionally with an on/off switch in its header."""

    def __init__(self, title: str, toggle: Optional[bool] = None, on_toggle=None):
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
        self.switch = None
        if toggle is not None:
            sw = self.switch = Toggle(toggle)
            sw.setAccessibleName(title)
            sw.toggled.connect(self.body.setEnabled)
            if on_toggle:
                sw.toggled.connect(on_toggle)
            self.body.setEnabled(toggle)
            head.addWidget(sw)
        lay.addLayout(head)
        lay.addWidget(self.body)

    def row(self, text: str, widget: QWidget) -> QWidget:
        r = QHBoxLayout()
        r.setSpacing(12)
        r.addWidget(QLabel(text), 1)
        r.addWidget(widget, 0)
        widget.setAccessibleName(text)
        self.form.addLayout(r)
        return widget

    def wide(self, text: str, widget: QWidget) -> QWidget:
        self.form.addWidget(label(text, "muted"))
        self.form.addWidget(widget)
        widget.setAccessibleName(text)
        return widget
