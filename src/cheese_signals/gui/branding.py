"""KPS mark, drawn in code so no binary asset has to ship or be regenerated.

The icon is a gold "KPS" monogram over a candlestick pair -- one green, one red
-- on a dark rounded tile. Drawing it with QPainter means it scales cleanly to
every size Windows asks for (16px in the taskbar through 256px in Explorer)
without shipping a pile of pre-rendered files.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap

from . import theme

APP_NAME = "KPS"
APP_TAGLINE = "OTC · 1-MINUTE"
APP_LONG_NAME = "KPS Signals"


def render_mark(size: int = 256, with_text: bool = True,
                text: str = APP_NAME) -> QPixmap:
    """Draw the mark at ``size`` pixels square.

    ``text`` is the monogram. It is a parameter rather than a constant so the
    autobot can have a visibly different taskbar icon while sharing this
    drawing code -- two apps from the same repository with identical icons is
    how a user ends up starting the wrong one.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 256.0

    # Tile
    tile = QRectF(6 * s, 6 * s, 244 * s, 244 * s)
    bg = QLinearGradient(tile.topLeft(), tile.bottomRight())
    bg.setColorAt(0.0, QColor("#1A1710"))
    bg.setColorAt(1.0, QColor("#0A0907"))
    p.setBrush(QBrush(bg))
    p.setPen(QPen(QColor(theme.GOLD_DIM), 3 * s))
    p.drawRoundedRect(tile, 46 * s, 46 * s)

    # Candlesticks: green then red, wick + body.
    def candle(cx, top, bottom, body_top, body_bottom, colour):
        pen = QPen(QColor(colour), 4 * s)
        p.setPen(pen)
        p.drawLine(int(cx), int(top), int(cx), int(bottom))
        p.setBrush(QBrush(QColor(colour)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(
            QRectF(cx - 13 * s, body_top, 26 * s, body_bottom - body_top), 3 * s, 3 * s
        )

    candle(78 * s, 150 * s, 226 * s, 168 * s, 214 * s, theme.BUY)
    candle(178 * s, 128 * s, 214 * s, 146 * s, 200 * s, theme.SELL)

    if with_text:
        f = QFont()
        f.setFamily("Segoe UI")
        f.setPointSizeF(max(1.0, 74 * s * min(1.0, 3.0 / max(1, len(text)))))
        f.setBold(True)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3 * s)
        p.setFont(f)
        p.setPen(QPen(QColor(theme.GOLD)))
        p.drawText(
            QRectF(0, 42 * s, size, 78 * s),
            Qt.AlignmentFlag.AlignCenter,
            text,
        )
    p.end()
    return pm


def app_icon(text: str = APP_NAME) -> QIcon:
    """Multi-resolution icon for the window and Windows taskbar."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        # Below ~32px the monogram turns to mud; the candles alone stay legible.
        icon.addPixmap(render_mark(size, with_text=size >= 32, text=text))
    return icon


def write_ico(path: str, text: str = APP_NAME) -> str:
    """Write a Windows .ico for PyInstaller to embed in the executable."""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [render_mark(s, with_text=s >= 32, text=text).toImage() for s in sizes]

    # Qt cannot write multi-image .ico, so build the container by hand.
    import struct
    from PySide6.QtCore import QBuffer, QByteArray

    pngs = []
    for img in images:
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        pngs.append(bytes(ba))

    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blobs = b"", b""
    for size, data in zip(sizes, pngs):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)

    with open(path, "wb") as fh:
        fh.write(header + entries + blobs)
    return path
