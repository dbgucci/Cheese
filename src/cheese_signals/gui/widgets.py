"""Reusable UI components: stat strip, signal cards, status dot, empty state.

Everything here is flat. No component draws a shadow or an all-round border
unless it is a real container -- depth comes from one surface step and a
hairline, which is what keeps a dense trading UI from looking like a form.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme


def hairline(vertical: bool = False) -> QFrame:
    line = QFrame()
    line.setObjectName("StatDivider")
    if vertical:
        line.setFixedWidth(1)
    else:
        line.setFixedHeight(1)
    return line


class StatTile(QFrame):
    """One metric inside a StatStrip: value, label, optional sub-note."""

    def __init__(self, label: str, value: str = "--", delta: str = ""):
        super().__init__()
        self.setObjectName("StatTile")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(4)

        self.label = QLabel(label.upper())
        self.label.setObjectName("StatLabel")

        self.value = QLabel(value)
        self.value.setObjectName("StatValue")

        # The delta row always exists, even when empty, so the big numbers sit
        # on one baseline across the strip.
        self.delta = QLabel(delta or " ")
        self.delta.setObjectName("StatDelta")

        lay.addWidget(self.label)
        lay.addWidget(self.value)
        lay.addWidget(self.delta)

    def set_value(self, value: str, delta: str = "", accent: str | None = None) -> None:
        self.value.setText(value)
        self.value.setStyleSheet(f"color: {accent};" if accent else "")
        self.delta.setText(delta or " ")


class StatStrip(QFrame):
    """Tiles sharing one surface, divided by hairlines.

    One bounded strip rather than four floating cards: the numbers read as a
    single summary line instead of four unrelated widgets.
    """

    def __init__(self, tiles: list[StatTile]):
        super().__init__()
        self.setObjectName("StatStrip")
        self.tiles = tiles

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        for i, tile in enumerate(tiles):
            if i:
                lay.addWidget(hairline(vertical=True))
            lay.addWidget(tile, 1)


class SignalCard(QFrame):
    """A live signal: pair, side, countdown to entry, expiry, confidence, rationale."""

    def __init__(self, signal):
        super().__init__()
        self.signal = signal
        is_buy = signal.direction == 1
        self.setObjectName("SignalCardBuy" if is_buy else "SignalCardSell")
        self.setMinimumHeight(118)

        root = QHBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(20)

        # --- left: pair, side pill, details ---
        left = QVBoxLayout()
        left.setSpacing(9)

        top = QHBoxLayout()
        top.setSpacing(10)
        pair = QLabel(signal.asset.replace("_otc", " OTC").upper())
        pair.setObjectName("SignalPair")
        pill = QLabel("BUY / CALL" if is_buy else "SELL / PUT")
        pill.setObjectName("PillBuy" if is_buy else "PillSell")
        pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        conf = QLabel(f"{signal.score:.0%} confidence")
        conf.setObjectName("PillNeutral")
        conf.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(pair)
        top.addWidget(pill)
        top.addWidget(conf)
        top.addStretch(1)

        entry = signal.entry_at.astimezone(timezone.utc)
        expiry = signal.expiry_at.astimezone(timezone.utc)
        meta = QLabel(
            f"Enter {entry:%H:%M:%S} UTC   ·   Expiry {expiry:%H:%M:%S} UTC   ·   {signal.strategy}"
        )
        meta.setObjectName("SignalMeta")

        reason = QLabel(signal.reason)
        reason.setObjectName("SignalReason")
        reason.setWordWrap(True)

        left.addLayout(top)
        left.addWidget(meta)
        left.addWidget(reason)

        # --- right: live countdown ---
        right = QVBoxLayout()
        right.setSpacing(3)
        right.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.countdown_label = QLabel("ENTER IN")
        self.countdown_label.setObjectName("CountdownLabel")
        self.countdown_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.countdown = QLabel("--:--")
        self.countdown.setObjectName("Countdown")
        self.countdown.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(self.countdown_label)
        right.addWidget(self.countdown)

        root.addLayout(left, 1)
        root.addLayout(right)

        self.refresh()

    def refresh(self) -> None:
        now = datetime.now(timezone.utc)
        status = getattr(self.signal, "status", "pending")

        if status == "cancelled":
            self.countdown_label.setText("CANCELLED")
            self.countdown.setText("--:--")
            self.countdown.setStyleSheet(f"color: {theme.TEXT_FAINT};")
            return

        secs_to_entry = self.signal.seconds_until_entry(now)
        if secs_to_entry > 0:
            self.countdown_label.setText("ENTER IN")
            self.countdown.setStyleSheet(f"color: {theme.GOLD};")
            self.countdown.setText(self._fmt(secs_to_entry))
            return

        secs_to_expiry = self.signal.seconds_until_expiry(now)
        if secs_to_expiry > 0:
            self.countdown_label.setText("EXPIRES IN")
            self.countdown.setStyleSheet(f"color: {theme.WARN};")
            self.countdown.setText(self._fmt(secs_to_expiry))
            return

        self.countdown_label.setText("SETTLING")
        self.countdown.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.countdown.setText("00:00")

    @staticmethod
    def _fmt(seconds: float) -> str:
        s = max(int(seconds), 0)
        return f"{s // 60:02d}:{s % 60:02d}"


class StatusDot(QLabel):
    """Small coloured dot indicating engine run state."""

    def __init__(self, size: int = 8):
        super().__init__()
        self._size = size
        self.setFixedSize(size, size)
        self.set_state(False)

    def set_state(self, running: bool) -> None:
        from PySide6.QtGui import QColor, QPainter, QPixmap

        colour = QColor(theme.BUY if running else theme.NEUTRAL)
        pix = QPixmap(self._size, self._size)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(colour)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(0, 0, self._size, self._size)
        p.end()
        self.setPixmap(pix)


class EmptyState(QWidget):
    """Shown where a list has no content yet.

    Unbounded on purpose -- an empty state inside a drawn box reads as a
    broken panel, whereas centred text on the page reads as "nothing here
    yet", which is what it means.
    """

    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(30, 70, 30, 70)
        lay.setSpacing(10)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        t = QLabel(title)
        t.setObjectName("SectionTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)

        if subtitle:
            s = QLabel(subtitle)
            s.setObjectName("Hint")
            s.setAlignment(Qt.AlignmentFlag.AlignCenter)
            s.setWordWrap(True)
            # A wrapped QLabel's size hint is unreliable, so pin both ends of
            # the measure: wide enough not to become a ribbon, narrow enough
            # to stay a readable column, tall enough not to clip.
            s.setMinimumWidth(440)
            s.setMaximumWidth(560)
            s.setMinimumHeight(72)
            s.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding)
            lay.addWidget(s, 0, Qt.AlignmentFlag.AlignHCenter)
