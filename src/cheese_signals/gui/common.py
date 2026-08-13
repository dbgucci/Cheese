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
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
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


class FittedTable(QTableWidget):
    """A fixed-metric table whose columns are never narrower than their headers.

    Column sizing is never ``ResizeToContents``. Doing that on a populated,
    visible table re-measures every cell on every write, which is the bug that
    once took this app's History tab to ninety seconds to open. So the widths
    are given in pixels -- and a pixel width that fits on one machine elides on
    another, because the header font is not the same width everywhere. A 160px
    column that held "Range window" on Linux rendered it "!ange windov" on
    Windows, where the same request needs 162px.

    The widths passed in are therefore treated as a request, and raised if the
    header text needs more. Measured on ``showEvent`` rather than in the
    constructor: the font comes from the stylesheet, which Qt does not apply
    until the widget is polished, so measuring any earlier measures the wrong
    font.
    """

    def __init__(self, headers: list[str], stretch: int, row_height: int,
                 widths: dict[int, int]):
        super().__init__(0, len(headers))
        self._asked = dict(widths)
        self._stretch = stretch
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(row_height)
        self.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        header = self.horizontalHeader()
        for i in range(len(headers)):
            header.setSectionResizeMode(
                i, QHeaderView.ResizeMode.Stretch if i == stretch
                else QHeaderView.ResizeMode.Fixed)
            if i != stretch:
                self.setColumnWidth(i, self._asked.get(i, 100))

    def showEvent(self, event):        # noqa: N802 - Qt naming
        super().showEvent(event)
        self.fit_headers()

    def fit_headers(self) -> None:
        metrics = self.horizontalHeader().fontMetrics()
        for i in range(self.columnCount()):
            if i == self._stretch:
                continue
            item = self.horizontalHeaderItem(i)
            if item is None:
                continue
            # 24px of slack for the section's own padding and the sort-indicator
            # gap; header text is centred, so it needs room on both sides.
            needed = metrics.horizontalAdvance(item.text()) + 24
            self.setColumnWidth(i, max(self._asked.get(i, 100), needed))


def table(headers: list[str], stretch: int = 0, row_height: int = 34,
          widths: dict[int, int] | None = None) -> QTableWidget:
    return FittedTable(headers, stretch, row_height, widths or {})


def cell(text: str, colour: str | None = None, mono: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if colour:
        item.setForeground(QColor(colour))
    if mono:
        font = item.font()
        font.setFamily("Consolas")
        item.setFont(font)
    return item


def form_grid() -> QGridLayout:
    """A two-column grid for settings rows: label left, control right.

    A grid rather than one QHBoxLayout per row, which is what this page had
    first and what made it unreadable. Independent rows cannot agree on a column
    position, so every control floated to wherever its own label happened to
    end -- and a stretched label pushed each one to the far edge of a wide
    window, a screen's width from the text describing it.

    Neither column stretches; a third, empty column takes the slack. Giving the
    label column the stretch instead lets the controls -- which contain
    expanding line edits -- claim the surplus on a wide monitor, which squeezes
    the labels back down to their wrap width.
    """
    grid = QGridLayout()
    grid.setHorizontalSpacing(24)
    grid.setVerticalSpacing(12)
    grid.setColumnMinimumWidth(0, 430)
    grid.setColumnMinimumWidth(1, 380)
    grid.setColumnStretch(0, 0)
    grid.setColumnStretch(1, 0)
    grid.setColumnStretch(2, 1)
    return grid


def form_row(grid: QGridLayout, row: int, text: str, widget,
             hint: str = "") -> int:
    """Add one label/control row. Returns the next free row index."""
    label = QLabel(text)
    # No word wrap: the column is wide enough for these, and a wrapped label in
    # a grid reports a height the grid does not honour, which is where the
    # clipped single lines came from.
    label.setWordWrap(False)
    label.setMinimumHeight(26)
    if hint:
        label.setToolTip(hint)
        if not isinstance(widget, (QHBoxLayout, QVBoxLayout)):
            widget.setToolTip(hint)
    grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignLeft
                   | Qt.AlignmentFlag.AlignVCenter)
    if isinstance(widget, (QHBoxLayout, QVBoxLayout)):
        grid.addLayout(widget, row, 1)
    else:
        grid.addWidget(widget, row, 1)
    return row + 1


def form_note(grid: QGridLayout, row: int, text: str) -> int:
    """A wrapped explanation spanning both columns.

    Given an explicit minimum height because a wrapped QLabel does not propagate
    its height through a grid: without it, several lines of instructions collapse
    into one clipped line the moment the page is taller than the window.
    """
    label = QLabel(text)
    label.setObjectName("Hint")
    label.setWordWrap(True)
    # 21px per line, not the font's own leading: at 18 the numbered Telegram
    # steps rendered as a solid block with the descenders nearly touching the
    # next line.
    label.setMinimumHeight(21 * (text.count("\n") + 1))
    label.setAlignment(Qt.AlignmentFlag.AlignTop)
    grid.addWidget(label, row, 0, 1, 2)
    return row + 1


def scroll_host(inner: QWidget) -> QScrollArea:
    """Put a page inside a scroll area.

    Not optional for a settings page. Without it, content taller than the window
    is not scrolled but *compressed*: Qt shrinks every widget to fit, which clips
    multi-line labels to a single cut-off line and squashes spin boxes and
    buttons into slivers. It looks like broken styling and is actually a missing
    scroll area.
    """
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(inner)
    return area
