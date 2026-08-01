"""Reusable UI components: stat cards, signal cards, status dot."""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme


def card_shadow(widget: QWidget, blur: int = 26, alpha: int = 96) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setXOffset(0)
    effect.setYOffset(3)
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)


class StatCard(QFrame):
    """A headline metric: big value, small uppercase label, optional sub-note."""

    def __init__(self, label: str, value: str = "--", delta: str = "", accent: str | None = None):
        super().__init__()
        self.setObjectName("StatCard")
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        card_shadow(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 15, 18, 15)
        lay.setSpacing(5)

        self.label = QLabel(label.upper())
        self.label.setObjectName("StatLabel")

        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        if accent:
            self.value.setStyleSheet(f"color: {accent};")

        # Always occupy the delta row, even when empty, so the big numbers
        # sit on the same baseline across every card in the row.
        self.delta = QLabel(delta or " ")
        self.delta.setObjectName("StatDelta")

        lay.addWidget(self.label)
        lay.addWidget(self.value)
        lay.addStretch(1)
        lay.addWidget(self.delta)

    def set_value(self, value: str, delta: str = "", accent: str | None = None) -> None:
        self.value.setText(value)
        if accent:
            self.value.setStyleSheet(f"color: {accent};")
        self.delta.setText(delta or " ")


class SignalCard(QFrame):
    """A live signal: pair, side, countdown to entry, expiry, confidence, rationale."""

    def __init__(self, signal):
        super().__init__()
        self.signal = signal
        is_buy = signal.direction == 1
        self.setObjectName("SignalCardBuy" if is_buy else "SignalCardSell")
        self.setMinimumHeight(112)
        card_shadow(self, 22)

        root = QHBoxLayout(self)
        root.setContentsMargins(18, 15, 18, 15)
        root.setSpacing(16)

        # --- left: pair, side pill, details ---
        left = QVBoxLayout()
        left.setSpacing(7)

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
        right.setSpacing(2)
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
            self.countdown.setStyleSheet(f"color: {theme.ACCENT};")
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

    def __init__(self, size: int = 9):
        super().__init__()
        self._size = size
        self.setFixedSize(size, size)
        self.set_state(False)

    def set_state(self, running: bool) -> None:
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


class EmptyState(QFrame):
    """Shown where a list has no content yet."""

    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(30, 40, 30, 40)
        lay.setSpacing(9)
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
            # Cap the measure so the copy wraps into a readable column instead
            # of one long line, and let it claim the height that wrapping needs.
            s.setMaximumWidth(520)
            s.setMinimumHeight(52)
            s.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding)
            lay.addWidget(s, 0, Qt.AlignmentFlag.AlignHCenter)
