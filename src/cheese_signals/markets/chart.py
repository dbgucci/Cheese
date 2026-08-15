"""A picture of the setup, drawn at the moment the alert fires.

Why an image at all
-------------------
A text alert asks the reader to reconstruct the chart in their head from six
numbers, and the reconstruction is where it goes wrong -- particularly for
somebody reviewing a signal they missed, hours later, in another time zone, on a
platform whose clock is not the one in the message. A picture removes the whole
class of problem: the candles, the range, the level that broke and the two lines
that decide the trade are all in one frame, and the time is written on the axis
in as many clocks as the reader might own.

Drawn with QPainter rather than a plotting library. matplotlib would add tens of
megabytes to an executable whose entire job is to send messages, and a
candlestick chart is rectangles and lines -- the drawing code below is shorter
than the configuration a plotting library would need. PySide6 is already in the
build because the app has a window.

What is deliberately in the picture
-----------------------------------
* **The opening range**, shaded, with its high and low extended across the
  frame. Without it the alert is "we bought here", which is not a setup.
* **Stop and target**, so the risk is visible rather than arithmetic.
* **The times, in every frame at once**, along the bottom. The axis is the
  market's own clock, and the caption repeats the moment in UTC, in the
  broker's chart clock and in the reader's zone.
* **A reference**, matching the one in the text, so a picture forwarded on its
  own can still be traced back to the alert it came from.

Nothing here is a trade instruction and nothing here can place one; this module
draws pixels.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from .clock import SESSIONS, in_zone, zone_name
from .execution import BUY

# The window drawn around the signal. Enough before the open to see where price
# came from, enough after the signal that the bar is not jammed against the edge.
LEAD_MINUTES = 12
TAIL_MINUTES = 18

# Mirrors gui/theme.py by eye rather than by import: markets/ does not depend on
# gui/, and inverting that to share six colour constants would mean the console
# bot could not draw a chart without the window package installed.
BG = "#0d0f12"
PANEL = "#14171c"
GRID = "#1e2229"
TEXT = "#e8eaed"
MUTED = "#8b929b"
FAINT = "#5a616b"
GOLD = "#d4a24c"
UP = "#3fb37f"
DOWN = "#e05c5c"
STOP_C = "#e05c5c"
TARGET_C = "#3fb37f"
ENTRY_C = "#e8eaed"


class ChartUnavailable(RuntimeError):
    """Raised when the drawing stack is missing, with what to do about it."""


_app = None


def _ensure_app():
    """A QGuiApplication has to exist before any QImage or QFont is made.

    The windowed app already has one. The console bot does not, so one is made
    offscreen -- which needs no display, and is why a headless machine can still
    produce chart images.
    """
    global _app
    try:
        from PySide6.QtGui import QGuiApplication
    except ImportError as exc:      # pragma: no cover - environment dependent
        raise ChartUnavailable(
            "chart images need PySide6, which is not installed here. "
            "Install it with: pip install PySide6 (the windowed app already "
            "includes it)."
        ) from exc
    existing = QGuiApplication.instance()
    if existing is not None:
        return existing
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _app = QGuiApplication([])
    return _app


@dataclass
class Mark:
    """A labelled moment on the chart: the break, the retest, the exit."""

    at: datetime
    label: str
    colour: str = GOLD


def _window(bars: pd.DataFrame, session_open: datetime, focus: datetime,
            range_minutes: int) -> pd.DataFrame:
    start = min(session_open - timedelta(minutes=LEAD_MINUTES),
                focus - timedelta(minutes=45))
    end = focus + timedelta(minutes=TAIL_MINUTES)
    window = bars[(bars.index >= start) & (bars.index <= end)]
    if len(window) < 5:
        # A feed that only just started, or a very quiet instrument: draw what
        # there is rather than refusing, but never an empty frame.
        window = bars.tail(60)
    return window


def render(
    bars: pd.DataFrame,
    *,
    symbol: str,
    direction: int,
    title: str,
    entry: float,
    stop: float,
    target: float,
    range_low: float,
    range_high: float,
    session_open: datetime,
    range_minutes: int,
    focus: datetime,
    digits: int,
    caption: list[str],
    marks: Optional[list[Mark]] = None,
    session_key: str = "",
    session_tz: str = "UTC",
    reference: str = "",
    width: int = 1180,
    height: int = 660,
) -> bytes:
    """The setup as a PNG. Returns the bytes; writing them is the caller's job."""
    _ensure_app()
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QPainter, QPen,
                               QPolygonF)

    frame = _window(bars, session_open, focus, range_minutes)
    if frame.empty:
        raise ChartUnavailable(f"no bars to draw for {symbol}")

    pad_l, pad_r, pad_b = 20, 104, 78
    # The header grows with the caption; a fixed height cropped the last
    # time frame off the bottom of it.
    pad_t = 58 + 15 * len(caption[:6]) + 12
    plot = QRectF(pad_l, pad_t, width - pad_l - pad_r, height - pad_t - pad_b)

    highs, lows = frame["high"].to_numpy(), frame["low"].to_numpy()
    opens, closes = frame["open"].to_numpy(), frame["close"].to_numpy()
    stamps = list(frame.index)
    # The stop and the target are part of the picture even when price never went
    # near them: a chart cropped to the candles hides how far away the risk was.
    levels = [entry, stop, target]
    if range_minutes > 0:
        levels += [range_low, range_high]
    hi = max([float(highs.max())] + levels)
    lo = min([float(lows.min())] + levels)
    span = (hi - lo) or 1.0
    hi, lo = hi + span * 0.06, lo - span * 0.06
    span = hi - lo

    def y(price: float) -> float:
        return plot.bottom() - (price - lo) / span * plot.height()

    slot = plot.width() / max(len(stamps), 1)

    def x(index: int) -> float:
        return plot.left() + (index + 0.5) * slot

    def x_at(moment: datetime) -> float:
        """Where a moment sits, even if no bar carries exactly that stamp."""
        for i, ts in enumerate(stamps):
            if ts >= moment:
                return x(i)
        return plot.right()

    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(BG))
    p = QPainter(image)
    # try/finally, because an exception with the painter still active takes
    # the process down with "cannot destroy paint device that is being
    # painted" -- a segfault rather than a traceback, in a program whose job
    # is to keep running unattended. One malformed drawLine call proved it.
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        def font(size: int, bold: bool = False, mono: bool = False) -> QFont:
            f = QFont("Consolas" if mono else "Segoe UI", size)
            f.setBold(bold)
            return f

        def text(s: str, px: float, py: float, colour: str, f: QFont,
                 align_right: bool = False) -> None:
            p.setFont(f)
            p.setPen(QPen(QColor(colour)))
            metrics = p.fontMetrics()
            if align_right:
                px -= metrics.horizontalAdvance(s)
            p.drawText(QPointF(px, py), s)

        # ------------------------------------------------------------ the plot bed
        p.fillRect(plot, QColor(PANEL))

        # horizontal grid and the price scale on the right, where a chart puts it
        p.setFont(font(9, mono=True))
        for i in range(6):
            price = lo + span * i / 5.0
            py = y(price)
            p.setPen(QPen(QColor(GRID), 1))
            p.drawLine(QPointF(plot.left(), py), QPointF(plot.right(), py))
            text(f"{price:.{digits}f}", plot.right() + 8, py + 3, FAINT,
                 font(9, mono=True))

        # ------------------------------------------------------- the opening range
        # Drawn only when there is one. A result chart has no opening range to
        # show, and a box labelled "opening range" drawn around something else
        # is worse than no box.
        if range_minutes > 0:
            range_end = session_open + timedelta(minutes=range_minutes)
            rx0, rx1 = x_at(session_open), x_at(range_end)
            box = QRectF(rx0, y(range_high), max(rx1 - rx0, 2.0),
                         y(range_low) - y(range_high))
            p.fillRect(box, QColor(212, 162, 76, 28))
            p.setPen(QPen(QColor(GOLD), 1, Qt.PenStyle.DashLine))
            for level in (range_high, range_low):
                p.drawLine(QPointF(plot.left(), y(level)),
                           QPointF(plot.right(), y(level)))
            text(f"opening range  {range_low:.{digits}f} - {range_high:.{digits}f}",
                 rx0, y(range_high) - 8, GOLD, font(9, bold=True))

        # ------------------------------------------------------------- the candles
        body_w = max(slot * 0.62, 1.6)
        for i in range(len(stamps)):
            up = closes[i] >= opens[i]
            colour = QColor(UP if up else DOWN)
            cx = x(i)
            p.setPen(QPen(colour, 1))
            p.drawLine(QPointF(cx, y(float(highs[i]))), QPointF(cx, y(float(lows[i]))))
            top, bottom = y(max(opens[i], closes[i])), y(min(opens[i], closes[i]))
            p.fillRect(QRectF(cx - body_w / 2, top, body_w,
                              max(bottom - top, 1.0)), QBrush(colour))

        # --------------------------------------------------- entry, stop and target
        for price, colour, label in ((entry, ENTRY_C, "entry"),
                                     (stop, STOP_C, "stop"),
                                     (target, TARGET_C, "target")):
            py = y(price)
            p.setPen(QPen(QColor(colour), 1.4,
                          Qt.PenStyle.SolidLine if label == "entry"
                          else Qt.PenStyle.DashLine))
            p.drawLine(QPointF(plot.left(), py), QPointF(plot.right(), py))
            tag = f"{label} {price:.{digits}f}"
            p.setFont(font(9, bold=True))
            w = p.fontMetrics().horizontalAdvance(tag) + 10
            p.fillRect(QRectF(plot.right() - w - 6, py - 9, w, 17), QColor(PANEL))
            text(tag, plot.right() - w - 1, py + 4, colour, font(9, bold=True))

        # ------------------------------------------------------------- the markers
        for mark in marks or []:
            mx = x_at(mark.at)
            p.setPen(QPen(QColor(mark.colour), 1.2, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(mx, plot.top()), QPointF(mx, plot.bottom()))
            arrow = QPolygonF([QPointF(mx, plot.top() + 12),
                               QPointF(mx - 6, plot.top() + 2),
                               QPointF(mx + 6, plot.top() + 2)])
            p.setBrush(QBrush(QColor(mark.colour)))
            p.setPen(QPen(QColor(mark.colour)))
            p.drawPolygon(arrow)
            p.setBrush(Qt.BrushStyle.NoBrush)
            text(mark.label, mx + 9, plot.top() + 13, mark.colour, font(9, bold=True))

        # ------------------------------------------------------------- the x axis
        #
        # Labelled in the market's own clock, because that is the one the session is
        # defined in -- and said out loud underneath, because an unlabelled axis is
        # how the reader ends up an hour out without knowing it.
        spec = SESSIONS.get(session_key)
        tz = spec.tz if spec is not None else session_tz
        step = max(len(stamps) // 7, 1)
        p.setPen(QPen(QColor(GRID), 1))
        for i in range(0, len(stamps), step):
            p.drawLine(QPointF(x(i), plot.top()), QPointF(x(i), plot.bottom()))
            local = in_zone(stamps[i], tz)
            text(f"{local:%H:%M}", x(i) - 14, plot.bottom() + 16, FAINT, font(9, mono=True))
        text(f"axis: {zone_name(tz)} time", plot.left(), plot.bottom() + 34, MUTED,
             font(9))

        # ---------------------------------------------------------------- headings
        side = "BUY" if direction == BUY else "SELL"
        head = font(17, bold=True)
        text(title, pad_l, 34, TEXT, head)
        p.setFont(head)
        # Measured, not estimated: len(title) * 10 drew "BUY" on top of "XAUUSD".
        text(side, pad_l + p.fontMetrics().horizontalAdvance(title) + 14, 34,
             UP if direction == BUY else DOWN, head)
        if reference:
            text(reference, width - pad_r + 84, 34, FAINT, font(10, mono=True),
                 align_right=True)

        for row, line in enumerate(caption[:6]):
            text(line, pad_l, 58 + row * 15, MUTED if row else TEXT, font(9.5))

        text("ORB Signals  ·  break and retest  ·  a record of the setup, not advice",
             pad_l, height - 16, FAINT, font(8.5))

    finally:
        if p.isActive():
            p.end()

    from PySide6.QtCore import QBuffer, QByteArray

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def render_signal(bars: pd.DataFrame, signal, **kwargs) -> bytes:
    """The chart for a BREAK or RETEST alert."""
    marks = [Mark(signal.at, signal.kind.upper())]
    return render(
        bars, symbol=signal.symbol, direction=signal.direction,
        title=signal.symbol, entry=signal.entry, stop=signal.stop,
        target=signal.target, range_low=signal.range_low,
        range_high=signal.range_high, session_open=signal.session_open,
        range_minutes=signal.range_minutes, focus=signal.at,
        digits=signal.digits, caption=signal.when_lines(), marks=marks,
        session_key=signal.session_key, session_tz=signal.session_tz,
        reference=signal.ref, **kwargs)


def render_outcome(bars: pd.DataFrame, outcome, **kwargs) -> bytes:
    """The chart for a finished paper trade: entry marked, exit marked."""
    colour = {"win": TARGET_C, "loss": STOP_C}.get(outcome.result, GOLD)
    marks = [Mark(outcome.opened_at, "ENTRY", GOLD),
             Mark(outcome.closed_at, outcome.result.upper(), colour)]
    return render(
        bars, symbol=outcome.symbol, direction=outcome.direction,
        title=f"{outcome.symbol}  {outcome.r_net:+.2f}R",
        entry=outcome.entry, stop=outcome.stop, target=outcome.target,
        range_low=min(outcome.entry, outcome.stop),
        range_high=max(outcome.entry, outcome.stop),
        session_open=outcome.opened_at, range_minutes=0,
        focus=outcome.closed_at, digits=outcome.digits,
        caption=outcome.when_lines(), marks=marks,
        session_key=outcome.session_key, session_tz=outcome.session_tz,
        reference=outcome.ref, **kwargs)
