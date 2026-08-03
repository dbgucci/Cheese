"""Line icons drawn with QPainter.

Drawn rather than shipped as files: a frozen exe would otherwise need image
assets bundled and path-resolved at runtime, and these are simple enough that
vector strokes cost less than that plumbing. Each icon is drawn on a 24x24
grid and scaled, so they stay crisp at any size.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

GRID = 24.0


def _pulse(p: QPainter) -> None:
    """Live signals: a heartbeat line."""
    path = QPainterPath(QPointF(2, 12))
    for x, y in [(7, 12), (9.5, 5), (13, 19), (15.5, 12), (22, 12)]:
        path.lineTo(x, y)
    p.drawPath(path)


def _clock(p: QPainter) -> None:
    """History: a clock face."""
    p.drawEllipse(QRectF(2.5, 2.5, 19, 19))
    p.drawLine(QPointF(12, 7), QPointF(12, 12.5))
    p.drawLine(QPointF(12, 12.5), QPointF(16, 15))


def _bars(p: QPainter) -> None:
    """Analytics: a bar chart."""
    for x, top in ((4.5, 14), (9.5, 8.5), (14.5, 11), (19.5, 4.5)):
        p.drawLine(QPointF(x, top), QPointF(x, 20))


def _waveform(p: QPainter) -> None:
    """Diagnostics: a signal trace stepping through levels."""
    path = QPainterPath(QPointF(2.5, 16))
    for x, y in [(6, 16), (6, 9), (10, 9), (10, 18), (14, 18), (14, 6), (18, 6), (18, 13), (21.5, 13)]:
        path.lineTo(x, y)
    p.drawPath(path)


def _sliders(p: QPainter) -> None:
    """Settings: three faders."""
    for y, knob in ((6.5, 15.5), (12, 8.5), (17.5, 13.5)):
        p.drawLine(QPointF(3, y), QPointF(21, y))
        p.setBrush(p.pen().color())
        p.drawEllipse(QPointF(knob, y), 2.4, 2.4)
        p.setBrush(Qt.BrushStyle.NoBrush)


DRAWERS = {
    "pulse": _pulse,
    "clock": _clock,
    "bars": _bars,
    "waveform": _waveform,
    "sliders": _sliders,
}


def pixmap(name: str, size: int, colour: str, width: float = 1.7) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / GRID, size / GRID)

    pen = QPen(QColor(colour))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    DRAWERS[name](p)
    p.end()
    return pix


def icon(name: str, colour: str, active_colour: str | None = None, size: int = 22) -> QIcon:
    """An icon that brightens when its button is checked or hovered.

    Qt swaps the ``Selected``/``Active`` pixmaps itself, so the nav buttons do
    not need per-state repainting code.
    """
    result = QIcon()
    result.addPixmap(pixmap(name, size, colour), QIcon.Mode.Normal, QIcon.State.Off)
    bright = active_colour or colour
    for mode in (QIcon.Mode.Selected, QIcon.Mode.Active):
        result.addPixmap(pixmap(name, size, bright, width=2.0), mode, QIcon.State.Off)
        result.addPixmap(pixmap(name, size, bright, width=2.0), mode, QIcon.State.On)
    result.addPixmap(pixmap(name, size, bright, width=2.0), QIcon.Mode.Normal, QIcon.State.On)
    return result
