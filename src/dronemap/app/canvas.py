"""The layout editor canvas: a video frame with draggable, resizable widget boxes."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QGraphicsObject, QGraphicsView

from .theme import T


class BoxItem(QGraphicsObject):
    """A movable, resizable box showing a rendered widget."""

    committed = Signal()  # emitted when a drag or resize finishes
    HANDLE = 9

    def __init__(self, name: str, title: str, frame: QRectF):
        super().__init__()
        self.name, self.title, self.frame = name, title, frame
        self.rect = QRectF()
        self.pix = None
        self.pix_size = None  # box size the pixmap was rendered for
        self.pix_margin = 0.0  # shadow margin around the box, in scene px
        self.mode = None
        self.hover = False
        self.setAcceptHoverEvents(True)
        self.setZValue(10)

    def set_rect(self, r: QRectF) -> None:
        self.prepareGeometryChange()
        self.rect = QRectF(r)
        self.update()

    def set_pixmap(self, pix, margin: float) -> None:
        self.prepareGeometryChange()
        self.pix, self.pix_margin, self.pix_size = pix, margin, (self.rect.width(), self.rect.height())
        self.update()

    def boundingRect(self) -> QRectF:
        m = max(self.pix_margin * 2, 0) + self.HANDLE * 2 + 4
        return self.rect.adjusted(-m, -m - 26, m, m)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        h = self.HANDLE * 1.4
        path.addRect(self.rect.adjusted(-h, -h, h, h))
        return path

    def paint(self, p: QPainter, opt, widget=None) -> None:
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.pix is not None and self.pix_size:
            # While resizing, stretch the last render until the new one arrives.
            sx = self.rect.width() / max(1e-6, self.pix_size[0])
            sy = self.rect.height() / max(1e-6, self.pix_size[1])
            m = self.pix_margin
            target = QRectF(
                self.rect.x() - m * sx, self.rect.y() - m * sy, self.rect.width() + 2 * m * sx, self.rect.height() + 2 * m * sy
            )
            p.drawPixmap(target, self.pix, QRectF(self.pix.rect()))
        if not (self.hover or self.mode is not None):
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
        tag = QRectF(self.rect.x(), self.rect.y() - 24, p.fontMetrics().horizontalAdvance(text) + 16, 18)
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

    def hoverMoveEvent(self, e) -> None:
        c = self._corner_at(e.pos())
        if c is not None:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor if c in (0, 3) else Qt.CursorShape.SizeBDiagCursor)
        elif self.rect.contains(e.pos()):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()

    def hoverEnterEvent(self, e) -> None:
        self.hover = True
        self.update()

    def hoverLeaveEvent(self, e) -> None:
        self.hover = False
        self.unsetCursor()
        self.update()

    def mousePressEvent(self, e) -> None:
        c = self._corner_at(e.pos())
        self.mode = ("resize", c) if c is not None else ("move", None)
        self.start_pos = e.scenePos()
        self.start_rect = QRectF(self.rect)
        e.accept()

    def mouseMoveEvent(self, e) -> None:
        if not self.mode:
            return
        d = e.scenePos() - self.start_pos
        r = QRectF(self.start_rect)
        F = self.frame
        min_side = min(F.width(), F.height()) * 0.04
        if self.mode[0] == "move":
            r.translate(d)
            r.moveLeft(min(max(F.left(), r.left()), F.right() - r.width()))
            r.moveTop(min(max(F.top(), r.top()), F.bottom() - r.height()))
            r = self._snap(r)
        else:
            c = self.mode[1]
            if c in (0, 2):
                r.setLeft(min(max(F.left(), r.left() + d.x()), r.right() - min_side))
            else:
                r.setRight(max(min(F.right(), r.right() + d.x()), r.left() + min_side))
            if c in (0, 1):
                r.setTop(min(max(F.top(), r.top() + d.y()), r.bottom() - min_side))
            else:
                r.setBottom(max(min(F.bottom(), r.bottom() + d.y()), r.top() + min_side))
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier and self.start_rect.height() > 0:
                ratio = self.start_rect.width() / self.start_rect.height()
                if c in (0, 1):
                    r.setTop(r.bottom() - r.width() / ratio)
                else:
                    r.setBottom(r.top() + r.width() / ratio)
        self.set_rect(r)

    def _snap(self, r: QRectF) -> QRectF:
        """Gentle snapping to the safe margins and centre lines."""
        F = self.frame
        S = min(F.width(), F.height())
        tol, margin = S * 0.012, S * 0.044
        for target, cur in ((F.left() + margin, r.left()), (F.right() - margin, r.right()), (F.center().x(), r.center().x())):
            if abs(target - cur) < tol:
                r.translate(target - cur, 0)
                break
        for target, cur in ((F.top() + margin, r.top()), (F.bottom() - margin, r.bottom()), (F.center().y(), r.center().y())):
            if abs(target - cur) < tol:
                r.translate(0, target - cur)
                break
        return r

    def mouseReleaseEvent(self, e) -> None:
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

    def restyle(self) -> None:
        self.setBackgroundBrush(QColor(T["canvas"]))
        self.setStyleSheet("QGraphicsView { background: %s; border-radius: 14px; }" % T["canvas"])

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.fit()

    def fit(self) -> None:
        r = self.scene().sceneRect()
        if r.width() > 0:
            self.fitInView(r.adjusted(-16, -34, 16, 16), Qt.AspectRatioMode.KeepAspectRatio)
