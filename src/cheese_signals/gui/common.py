"""Page furniture shared by the windows in this app.

Extracted so a second window cannot drift from the first. The spacing grid and
palette live in ``theme``; these are the three or four assemblies that every page
is built out of, and duplicating them per window is how one window ends up with
24px card padding and the other with 20.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme


def page_layout(widget: QWidget) -> QVBoxLayout:
    lay = QVBoxLayout(widget)
    lay.setContentsMargins(theme.PAGE_MARGIN_H, theme.PAGE_MARGIN_TOP,
                           theme.PAGE_MARGIN_H, theme.PAGE_MARGIN_BOTTOM)
    lay.setSpacing(theme.GAP_LG)
    return lay


def title_block(title: str, subtitle: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(5)
    t = QLabel(title)
    t.setObjectName("PageTitle")
    s = QLabel(subtitle)
    s.setObjectName("PageSubtitle")
    s.setWordWrap(True)
    s.setMaximumWidth(660)
    # A wrapped label's height is not propagated through the enclosing
    # QHBoxLayout, so the second line is clipped by whatever sits below.
    # Reserving two lines is deterministic; measuring is not.
    s.setMinimumHeight(38)
    s.setAlignment(Qt.AlignmentFlag.AlignTop)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


def page_header(title: str, subtitle: str, actions: list[QWidget]) -> QHBoxLayout:
    """Title on the left, actions bottom-aligned on the right."""
    row = QHBoxLayout()
    row.setSpacing(10)
    row.addWidget(title_block(title, subtitle), 1)
    row.addSpacing(20)
    for w in actions:
        row.addWidget(w, 0, Qt.AlignmentFlag.AlignBottom)
    return row


def card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(24, 20, 24, 20)
    lay.setSpacing(12)
    if title:
        label = QLabel(title)
        label.setObjectName("SectionTitle")
        lay.addWidget(label)
    return frame, lay


def table(headers: list[str], stretch: int = 0, row_height: int = 34,
          widths: dict[int, int] | None = None) -> QTableWidget:
    """A fixed-metric table.

    Column sizing is never ``ResizeToContents``. Doing that on a populated,
    visible table re-measures every cell on every write, which is the bug that
    once took this app's History tab to ninety seconds to open.
    """
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(row_height)
    t.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setShowGrid(False)
    header = t.horizontalHeader()
    for i in range(len(headers)):
        header.setSectionResizeMode(
            i, QHeaderView.ResizeMode.Stretch if i == stretch
            else QHeaderView.ResizeMode.Fixed)
        if i != stretch:
            t.setColumnWidth(i, (widths or {}).get(i, 100))
    return t


def cell(text: str, colour: str | None = None, mono: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if colour:
        item.setForeground(QColor(colour))
    if mono:
        font = item.font()
        font.setFamily("Consolas")
        item.setFont(font)
    return item


def labelled(text: str, widget, label_width: int = 300,
             control_width: int = 260) -> QHBoxLayout:
    """A settings row: description on the left, control on the right."""
    row = QHBoxLayout()
    row.setSpacing(12)
    label = QLabel(text)
    label.setObjectName("Hint")
    label.setMinimumWidth(label_width)
    label.setWordWrap(True)
    row.addWidget(label, 1)
    if isinstance(widget, (QHBoxLayout, QVBoxLayout)):
        row.addLayout(widget, 1)
    else:
        if control_width:
            widget.setFixedWidth(control_width)
        row.addWidget(widget, 0)
    return row
