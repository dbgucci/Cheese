"""Table models for the history view.

Why a model and not a QTableWidget: QTableWidget stores a widget-item object
per cell, and populating one that is already laid out inside a visible window
costs a full layout pass *per cell*. Measured on this app at 500 rows x 8
columns, that was 23 ms per ``setItem`` -- 91 seconds to open the History tab,
which is what "the more history I collect, the more it freezes" actually was.

A QAbstractTableModel stores nothing per cell. The view asks for the handful
of rows it is about to paint and nothing else, so opening the tab costs the
same whether the page holds 50 rows or 50,000.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

from . import theme

COLUMNS = ("Time (UTC)", "Pair", "Side", "Conf.", "Setup", "Result", "P/L", "Why")

# Starting widths in pixels; the last column takes the remaining space. Fixed
# up front rather than measured from content, because measuring content is
# precisely what made the old table unusable. Sized for the widest real value
# each column holds -- a full ISO timestamp, "GBPUSD OTC", "-10.00" -- since
# nothing here will auto-fit them later.
COLUMN_WIDTHS = (200, 142, 70, 68, 176, 88, 92)

TIME, PAIR, SIDE, CONF, SETUP, RESULT, PNL, WHY = range(8)


class TradeTableModel(QAbstractTableModel):
    """Settled trades, newest first."""

    def __init__(self, rows: list[dict[str, Any]] | None = None):
        super().__init__()
        self._rows: list[dict[str, Any]] = rows or []
        self._bold = QFont()
        self._bold.setBold(True)
        self._buy = QColor(theme.BUY_BRIGHT)
        self._sell = QColor(theme.SELL_BRIGHT)
        self._muted = QColor(theme.TEXT_MUTED)
        self._faint = QColor(theme.TEXT_FAINT)

    # ------------------------------ contents ------------------------------
    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, index: int) -> dict[str, Any] | None:
        return self._rows[index] if 0 <= index < len(self._rows) else None

    # ------------------------- QAbstractTableModel ------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        # Headers align with the values beneath them: numbers right, everything
        # else left. Qt centres them by default, which reads as misaligned.
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if section in (CONF, PNL):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        won = bool(row.get("won"))

        if role == Qt.ItemDataRole.DisplayRole:
            return self._text(row, col, won)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col in (RESULT, PNL):
                return self._buy if won else self._sell
            if col in (SETUP, WHY):
                return self._faint
            if col in (TIME, CONF):
                return self._muted
            return None

        if role == Qt.ItemDataRole.FontRole and col in (PAIR, RESULT):
            return self._bold

        if role == Qt.ItemDataRole.TextAlignmentRole and col in (CONF, PNL):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ToolTipRole and col == WHY:
            return str(row.get("outcome_reason") or "")

        return None

    @staticmethod
    def _text(row: dict[str, Any], col: int, won: bool) -> str:
        if col == TIME:
            return str(row.get("entry_at", ""))[:19].replace("T", " ")
        if col == PAIR:
            return str(row.get("asset", "")).replace("_otc", " OTC").upper()
        if col == SIDE:
            return "BUY" if row.get("direction") == 1 else "SELL"
        if col == CONF:
            return f"{float(row.get('score') or 0):.0%}"
        if col == SETUP:
            return str(row.get("strategy", ""))
        if col == RESULT:
            return "WIN" if won else "LOSS"
        if col == PNL:
            return f"{float(row.get('pnl') or 0):+.2f}"
        return str(row.get("outcome_reason") or "")


def matches(row: dict[str, Any], needle: str) -> bool:
    """Case-insensitive search across the fields the table shows."""
    if not needle:
        return True
    needle = needle.lower()
    haystack = " ".join(
        str(row.get(k) or "")
        for k in ("asset", "strategy", "outcome_reason", "entry_at", "session")
    )
    if needle in haystack.lower():
        return True
    # Let "buy"/"sell"/"win"/"loss" match the rendered words, which are
    # derived rather than stored.
    derived = ("buy" if row.get("direction") == 1 else "sell") + (
        " win" if row.get("won") else " loss"
    )
    return needle in derived
