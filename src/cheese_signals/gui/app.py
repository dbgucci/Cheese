"""Main window: Live signals, History, Analytics, Settings."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal as QtSignal
from PySide6.QtGui import QColor, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import analytics, paths, profiles, storage
from ..engine import SignalEngine
from ..settings import DEFAULT_ASSETS, Settings
from . import models, theme
from .branding import APP_LONG_NAME, APP_NAME, APP_TAGLINE, app_icon
from .icons import icon as nav_icon
from .settings_page import SettingsPage
from .widgets import EmptyState, SignalCard, StatStrip, StatTile, StatusDot, hairline

PAYOUT = 0.85

# Row caps. The journal is meant to grow for years; the views are not meant to
# read all of it. History renders through a model, so its cap is about how far
# back you'd scroll rather than about what the widget can survive.
HISTORY_ROWS = 5_000
ANALYTICS_ROWS = 20_000

# Vertical chrome of a _card() with a title: top+bottom margins, the
# section-title row, and the layout spacing between title and content.
_CARD_CHROME_H = 20 + 20 + 20 + 12

# Analytics slice tables: shorter rows than History, because these are dense
# summaries read at a glance rather than a list you scroll.
_ANALYTICS_ROW_H = 34


def _page_layout(widget: QWidget) -> QVBoxLayout:
    """Standard page padding and rhythm, from the spacing grid in `theme`."""
    lay = QVBoxLayout(widget)
    lay.setContentsMargins(
        theme.PAGE_MARGIN_H, theme.PAGE_MARGIN_TOP,
        theme.PAGE_MARGIN_H, theme.PAGE_MARGIN_BOTTOM,
    )
    lay.setSpacing(theme.GAP_LG)
    return lay


def _title_block(title: str, subtitle: str) -> QWidget:
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
    # QHBoxLayout, so the second line gets clipped by whatever sits below.
    # Reserving two lines up front is deterministic; measuring is not.
    s.setMinimumHeight(38)
    s.setAlignment(Qt.AlignmentFlag.AlignTop)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


def _page_header(title: str, subtitle: str, actions: list[QWidget]) -> QHBoxLayout:
    """Title on the left, actions bottom-aligned on the right."""
    row = QHBoxLayout()
    row.setSpacing(10)
    # The title block gets the stretch, so the subtitle wraps into a readable
    # measure instead of being squeezed into a column by the buttons.
    row.addWidget(_title_block(title, subtitle), 1)
    row.addSpacing(20)
    for w in actions:
        row.addWidget(w, 0, Qt.AlignmentFlag.AlignBottom)
    return row


def _card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
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


class LivePage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.cards: list[SignalCard] = []

        root = _page_layout(self)

        self.halt_btn = QPushButton("Halt Trading")
        self.halt_btn.setObjectName("Danger")
        self.halt_btn.setToolTip("Immediately stop placing new orders. Signals keep running.")
        self.halt_btn.clicked.connect(self.window.toggle_halt)
        self.halt_btn.setVisible(False)

        self.start_btn = QPushButton("Start Engine")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(self.window.toggle_engine)

        root.addLayout(
            _page_header(
                "Live Signals",
                "Signals are announced ahead of their entry minute and re-checked on every "
                "candle until then.",
                [self.halt_btn, self.start_btn],
            )
        )

        self.stat_pending = StatTile("Awaiting entry", "0")
        self.stat_today = StatTile("Signals today", "0")
        self.stat_winrate = StatTile("Win rate", "--", f"break-even {1 / (1 + PAYOUT):.1%}")
        self.stat_pnl = StatTile("Net P/L", "0.00")
        root.addWidget(
            StatStrip([self.stat_pending, self.stat_today, self.stat_winrate, self.stat_pnl])
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.cards_layout = QVBoxLayout(holder)
        self.cards_layout.setContentsMargins(0, 0, 8, 0)
        self.cards_layout.setSpacing(theme.GAP)

        self.empty = EmptyState(
            "No active signals",
            "Start the engine and KPS will watch your OTC pairs. When a setup forms you'll "
            "get the pair, direction, and the exact minute to enter — here and on Telegram.",
        )
        # Equal stretch on both, so the empty state sits in the upper middle
        # of the page rather than pinned under the stats. It is hidden once
        # cards exist, and the trailing stretch then keeps them top-aligned.
        self.cards_layout.addWidget(self.empty, 1)
        self.cards_layout.addStretch(1)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

    def add_signal(self, signal) -> None:
        self.empty.setVisible(False)
        card = SignalCard(signal)
        self.cards.insert(0, card)
        self.cards_layout.insertWidget(0, card)
        while len(self.cards) > 12:
            old = self.cards.pop()
            old.setParent(None)

    def refresh(self) -> None:
        for c in self.cards:
            c.refresh()
        if not self.cards:
            self.empty.setVisible(True)


class HistoryPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self._rows: list[dict] = []

        root = _page_layout(self)

        export = QPushButton("Export CSV")
        export.setObjectName("Ghost")
        export.clicked.connect(self.export_csv)
        folder = QPushButton("Open Data Folder")
        folder.setObjectName("Ghost")
        folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.data_dir())))
        )
        root.addLayout(
            _page_header(
                "Trade History",
                "Every settled signal with the reason it won or lost. Stored permanently in "
                "your data folder.",
                [export, folder],
            )
        )

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search pair, setup, or reason…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(340)
        self.search.textChanged.connect(self._apply_filter)
        controls.addWidget(self.search)
        controls.addStretch(1)
        self.count_label = QLabel()
        self.count_label.setObjectName("Hint")
        controls.addWidget(self.count_label)
        root.addLayout(controls)

        # A model/view, not a QTableWidget: see gui/models.py for why. The
        # view only asks for the rows it paints, so this stays instant as the
        # journal grows.
        self.model = models.TradeTableModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(theme.ROW_HEIGHT)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setHorizontalScrollMode(QTableView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QTableView.ScrollMode.ScrollPerPixel)

        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        # Fixed widths, never ResizeToContents: measuring every cell is what
        # made this tab take 90 seconds to open on a large journal.
        for i, width in enumerate(models.COLUMN_WIDTHS):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(i, width)
        hh.setSectionResizeMode(len(models.COLUMN_WIDTHS), QHeaderView.ResizeMode.Stretch)

        root.addWidget(self.table, 1)

    def refresh(self) -> None:
        self._rows = list(reversed(self.window.journal.joined_results(limit=HISTORY_ROWS)))
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self.search.text().strip()
        rows = [r for r in self._rows if models.matches(r, needle)] if needle else self._rows
        self.model.set_rows(rows)

        if not self._rows:
            self.count_label.setText("No settled trades yet.")
        elif needle:
            self.count_label.setText(f"{len(rows)} of {len(self._rows)} trades")
        else:
            self.count_label.setText(f"{len(rows)} trades")

    def export_csv(self) -> None:
        import csv

        default = str(paths.exports_dir() / f"trades_{datetime.now():%Y%m%d_%H%M}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export trades", default, "CSV (*.csv)")
        if not path:
            return
        rows = self.window.journal.joined_results()
        if not rows:
            QMessageBox.information(self, "Export", "No settled trades to export yet.")
            return
        keys = [k for k in rows[0].keys() if k != "features"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        QMessageBox.information(self, "Export", f"Exported {len(rows)} trades to:\n{path}")


class AnalyticsPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window

        root = _page_layout(self)

        self.filter_box = QComboBox()
        self.filter_box.addItems(analytics.FILTER_OPTIONS)
        self.filter_box.setMinimumWidth(160)
        self.filter_box.setToolTip(
            "Results from different builds are stored separately so a fix can be "
            "measured. 'This build only' hides trades produced by older versions."
        )
        self.filter_box.currentTextChanged.connect(lambda _: self.refresh())

        refresh = QPushButton("Refresh")
        refresh.setObjectName("Ghost")
        refresh.clicked.connect(self.refresh)

        root.addLayout(
            _page_header(
                "Analytics",
                "Realised performance from your own logged trades — sliced by the dimensions "
                "the engine can act on.",
                [self.filter_box, refresh],
            )
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.body = QVBoxLayout(holder)
        self.body.setContentsMargins(0, 0, 8, 0)
        self.body.setSpacing(theme.GAP)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

        self.summary_card, self.summary_layout = _card("What your data says")
        self.summary_text = QLabel("No trades logged yet.")
        self.summary_text.setObjectName("Hint")
        self.summary_text.setWordWrap(True)
        self.summary_layout.addWidget(self.summary_text)
        self.body.addWidget(self.summary_card)

        self.tables_holder = QVBoxLayout()
        self.tables_holder.setSpacing(14)
        self.body.addLayout(self.tables_holder)
        self.body.addStretch(1)
        self._table_widgets: list[QWidget] = []

    def refresh(self) -> None:
        all_rows = self.window.journal.joined_results(limit=ANALYTICS_ROWS)
        mode = self.filter_box.currentText() if hasattr(self, "filter_box") else analytics.FILTER_ALL
        rows = analytics.filter_rows(all_rows, mode)

        tips = analytics.suggestions(rows, payout=PAYOUT)
        provenance = (
            f"Showing: {mode} — {len(rows)} of {len(all_rows)} logged trades "
            f"({analytics.describe_versions(rows)})."
        )
        self.summary_text.setText(provenance + "\n\n" + "\n\n".join(f"• {t}" for t in tips))

        for w in self._table_widgets:
            w.setParent(None)
        self._table_widgets.clear()

        if not rows:
            return

        bd = analytics.breakdown(rows)
        be = analytics.breakeven_win_rate(PAYOUT)
        titles = {
            "strategy": "By setup",
            "session": "By session",
            "hour": "By hour (UTC)",
            "asset": "By pair",
            "lead": "By advance-warning time",
            "confidence": "By confidence bucket",
        }
        for dim, title in titles.items():
            slices = [s for s in bd.get(dim, []) if s.trades > 0][:12]
            if not slices:
                continue
            frame, lay = _card(title)
            table = QTableWidget(len(slices), 5)
            table.setHorizontalHeaderLabels(["", "Trades", "Win rate", "vs break-even", "Net P/L"])
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setDefaultSectionSize(_ANALYTICS_ROW_H)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setShowGrid(False)
            table.setFrameShape(QFrame.Shape.NoFrame)
            table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setHighlightSections(False)
            # Safe here, unlike in History: these tables are capped at 12 rows
            # and are built once per refresh, so measuring content is cheap.
            for i in range(1, 5):
                table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)

            positive = QColor(theme.BUY_BRIGHT)
            negative = QColor(theme.SELL_BRIGHT)
            for r, s in enumerate(slices):
                edge = s.win_rate - be
                cells = [
                    s.key,
                    str(s.trades),
                    f"{s.win_rate:.1%}",
                    f"{edge:+.1%}" + ("" if s.is_significant else "  (low sample)"),
                    f"{s.pnl:+.2f}",
                ]
                for c, v in enumerate(cells):
                    item = QTableWidgetItem(v)
                    if c == 3 and s.is_significant:
                        item.setForeground(positive if edge > 0 else negative)
                    table.setItem(r, c, item)

            # Header + rows, so the whole table is visible without its own
            # scrollbar.
            table_h = len(slices) * _ANALYTICS_ROW_H + 42
            table.setFixedHeight(table_h)
            table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            lay.addWidget(table)
            # These cards are added long after the scroll area was built, and
            # the holder does not re-propagate its minimum size on its own, so
            # the frames otherwise collapse to their margins. setFixedHeight
            # pins both the minimum and maximum, which the parent layout must
            # honour; a Fixed *size policy* would not work here because Qt then
            # sizes from sizeHint() and ignores an explicit minimum.
            frame.setFixedHeight(_CARD_CHROME_H + table_h)
            self.tables_holder.addWidget(frame)
            self._table_widgets.append(frame)

        losses = analytics.loss_reasons(rows)
        if losses:
            frame, lay = _card("Most common conditions in losing trades")
            text = QTextEdit()
            text.setReadOnly(True)
            text.setPlainText("\n".join(f"{n:>4}×  {reason}" for reason, n in losses))
            text_h = min(len(losses) * 20 + 26, 200)
            text.setFixedHeight(text_h)
            lay.addWidget(text)
            frame.setFixedHeight(_CARD_CHROME_H + text_h)
            self.tables_holder.addWidget(frame)
            self._table_widgets.append(frame)


class DiagnosticsPage(QWidget):
    """Live view of what the engine is reading and why it did or didn't fire."""

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.paused = False

        root = _page_layout(self)

        self.only_fired = QCheckBox("Signals only")
        self.only_fired.stateChanged.connect(lambda _: self.refresh())

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setObjectName("Ghost")
        self.pause_btn.clicked.connect(self._toggle_pause)

        clear = QPushButton("Clear")
        clear.setObjectName("Ghost")
        clear.clicked.connect(self._clear)

        logs = QPushButton("Open Log Folder")
        logs.setObjectName("Ghost")
        logs.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs_dir())))
        )

        root.addLayout(
            _page_header(
                "Diagnostics",
                "Every pair the engine reads, each condition it checks, and the reason a "
                "setup fired or did not. Written to a log file in your data folder too.",
                [self.only_fired, self.pause_btn, clear, logs],
            )
        )

        self.summary = QLabel("Engine not running.")
        self.summary.setObjectName("Hint")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        root.addWidget(self.view, 1)

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_btn.setText("Resume" if self.paused else "Pause")
        if not self.paused:
            self.refresh()

    def _clear(self) -> None:
        eng = self.window.engine
        if eng:
            eng.traces.clear()
        self.view.clear()

    # Outcomes that mean something is wrong, worst first. Listed ahead of the
    # routine counts, because "6 error" buried after "412 warming up" is the
    # line the user needed and did not read.
    PROBLEMS = ("ERROR", "BLOCKED", "CONFIG WARNING")

    def _set_summary(self, counts: dict) -> None:
        if not counts:
            self.summary.setText("Waiting for the first candle...")
            self.summary.setObjectName("Hint")
            self.summary.setStyleSheet("")
            return

        problems = [f"{counts[k]} {k.lower()}" for k in self.PROBLEMS if counts.get(k)]
        routine = [
            f"{v} {k.lower()}"
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
            if k not in self.PROBLEMS
        ]

        if problems:
            self.summary.setText(
                "⚠ " + ", ".join(problems) + " — details below.   "
                + ", ".join(routine)
            )
            self.summary.setStyleSheet(f"color: {theme.WARN}; font-weight: 600;")
        else:
            self.summary.setText("Recent activity: " + ", ".join(routine))
            self.summary.setStyleSheet("")

    def refresh(self) -> None:
        eng = self.window.engine
        if eng is None:
            self.summary.setText(
                "Engine not running. Start it from Live Signals and this will fill in."
            )
            self.summary.setStyleSheet("")
            return
        if self.paused:
            return

        traces = eng.traces.recent(limit=250, only_fired=self.only_fired.isChecked())
        self._set_summary(eng.traces.counts())

        # Preserve the scroll position unless the user is pinned to the bottom.
        bar = self.view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.view.setPlainText("\n".join(t.as_text() for t in traces))
        if at_bottom:
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())


class MainWindow(QMainWindow):
    sig_signal = QtSignal(object)
    sig_cancel = QtSignal(object, str)
    sig_result = QtSignal(object)
    sig_status = QtSignal(str)
    sig_error = QtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_LONG_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1240, 830)
        self.setMinimumSize(1060, 700)

        self.settings = Settings.load()
        self.journal = storage.Journal()
        self._history_stale = False
        self._status_text = ""
        self._last_error: str = ""
        self.engine: SignalEngine | None = None

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        content.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        self.live_page = LivePage(self)
        self.history_page = HistoryPage(self)
        self.analytics_page = AnalyticsPage(self)
        self.diagnostics_page = DiagnosticsPage(self)
        self.settings_page = SettingsPage(self)
        for p in (self.live_page, self.history_page, self.analytics_page,
                  self.diagnostics_page, self.settings_page):
            self.stack.addWidget(p)
        content.addWidget(self.stack, 1)
        outer.addLayout(content, 1)
        outer.addWidget(self._build_statusbar())

        self.sig_signal.connect(self._on_signal)
        self.sig_cancel.connect(self._on_cancel)
        self.sig_result.connect(self._on_result)
        self.sig_status.connect(self.set_status)
        self.sig_error.connect(self.show_error)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick_ui)
        self.timer.start(1000)

        self.history_page.refresh()
        self.analytics_page.refresh()
        self._refresh_stats()
        self.set_status(f"Data folder: {paths.data_dir()}")

    # ------------------------------ chrome ------------------------------
    def _build_sidebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(232)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(14, 30, 14, 20)
        lay.setSpacing(2)

        brandbox = QVBoxLayout()
        brandbox.setContentsMargins(12, 0, 12, 0)
        brandbox.setSpacing(3)
        brand = QLabel(APP_NAME)
        brand.setObjectName("BrandMark")
        sub = QLabel(APP_TAGLINE)
        sub.setObjectName("BrandSub")
        brandbox.addWidget(brand)
        brandbox.addWidget(sub)
        lay.addLayout(brandbox)
        lay.addSpacing(28)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for i, (label, glyph) in enumerate(
            [("Live Signals", "pulse"), ("History", "clock"), ("Analytics", "bars"),
             ("Diagnostics", "waveform"), ("Settings", "sliders")]
        ):
            btn = QPushButton(f"  {label}")
            btn.setObjectName("NavButton")
            btn.setIcon(nav_icon(glyph, theme.TEXT_MUTED, theme.TEXT))
            btn.setIconSize(QSize(21, 21))
            btn.setCheckable(True)
            btn.setChecked(i == 0)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, idx=i: self._navigate(idx))
            self.nav_group.addButton(btn, i)
            lay.addWidget(btn)

        lay.addStretch(1)
        lay.addWidget(hairline())
        lay.addSpacing(14)

        self.sidebar_note = QLabel("Engine stopped")
        self.sidebar_note.setObjectName("Hint")
        self.sidebar_note.setWordWrap(True)
        self.sidebar_note.setContentsMargins(12, 0, 12, 0)
        lay.addWidget(self.sidebar_note)
        return bar

    def _build_statusbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(38)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(9)

        self.status_dot = StatusDot()
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("StatusText")
        # Long messages must not push the clock off the bar; they are elided
        # here and readable in full via the Details button and Diagnostics.
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.status_dot)
        lay.addWidget(self.status_label, 1)

        self.details_btn = QPushButton("Details")
        self.details_btn.setObjectName("StatusLink")
        self.details_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details_btn.setVisible(False)
        self.details_btn.clicked.connect(self._show_last_error)
        lay.addWidget(self.details_btn)

        self.clock = QLabel()
        self.clock.setObjectName("StatusText")
        lay.addWidget(self.clock)
        return bar

    def _show_last_error(self) -> None:
        """The full text of the last error, selectable so it can be pasted."""
        box = QMessageBox(self)
        box.setWindowTitle("Engine error")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("The engine reported an error.")
        box.setInformativeText(
            "Every error is also written to Diagnostics and to the daily log "
            "file in your data folder."
        )
        box.setDetailedText(self._last_error or "(no details)")
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.exec()

    def _navigate(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if index == 1:
            self.history_page.refresh()
            self._history_stale = False
        elif index == 2:
            self.analytics_page.refresh()
        elif index == 3:
            self.diagnostics_page.refresh()

    # ------------------------------ engine ------------------------------
    def _feed_factory(self, asset: str):
        s = self.settings
        if s.data_source == "pocket_option":
            from ..data import get_pocket_option_feed

            return get_pocket_option_feed(
                asset, timeframe_seconds=s.timeframe_seconds, ssid=s.pocket_option_ssid or None
            )
        from ..data import SyntheticFeed

        return SyntheticFeed(seconds_per_candle=s.timeframe_seconds)

    def _notifier(self):
        s = self.settings
        if not (s.telegram_enabled and s.telegram_bot_token and s.telegram_chat_id):
            return None
        from ..notifiers import TelegramNotifier

        return TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)

    def toggle_engine(self) -> None:
        if self.engine and self.engine.is_running:
            self.engine.stop()
            self.engine = None
            self.live_page.start_btn.setText("Start Engine")
            self.live_page.start_btn.setObjectName("Primary")
            self.live_page.halt_btn.setVisible(False)
            self.status_dot.set_state(False)
            self.sidebar_note.setText("Engine stopped")
            self.set_status("Engine stopped")
            return

        # Drop any cached Pocket Option session so restarting the engine after
        # pasting a fresh SSID actually reconnects instead of reusing a dead one.
        if self.settings.data_source == "pocket_option":
            try:
                from ..data.pocket_option import reset_clients

                reset_clients()
            except ImportError:
                pass

        try:
            executor = None
            mode = getattr(self.settings, "trade_mode", "off")
            if mode != "off":
                from .. import execution
                client = None
                if mode == "live":
                    feed = self._feed_factory(self.settings.assets[0])
                    client = getattr(feed, "_client", None)
                executor = execution.build_executor(mode, self.settings, client)

            self.engine = SignalEngine(
                settings=self.settings,
                journal=self.journal,
                feed_factory=self._feed_factory,
                notifier=self._notifier(),
                payout=PAYOUT,
                on_signal=lambda s: self.sig_signal.emit(s),
                on_cancel=lambda s, r: self.sig_cancel.emit(s, r),
                on_result=lambda o: self.sig_result.emit(o),
                on_status=lambda m: self.sig_status.emit(m),
                on_error=lambda m: self.sig_error.emit(m),
            )
            self.engine.executor = executor
            self.engine.trade_mode = mode
            self.engine.start()
        except Exception as exc:
            QMessageBox.critical(self, "Engine", f"Could not start:\n{exc}")
            self.engine = None
            return

        self.live_page.start_btn.setText("Stop Engine")
        self.live_page.halt_btn.setVisible(mode != "off")
        self.status_dot.set_state(True)
        self.sidebar_note.setText(f"Watching {len(self.settings.assets)} pairs")
        self.set_status("Engine running")

    # ----------------------------- callbacks -----------------------------
    def toggle_halt(self) -> None:
        eng = self.engine
        if eng is None:
            return
        if eng.safety.state.halted:
            eng.safety.resume()
            self.live_page.halt_btn.setText("Halt Trading")
            self.set_status("Trading resumed")
        else:
            eng.safety.halt("halted manually")
            self.live_page.halt_btn.setText("Resume Trading")
            self.set_status("Trading halted — no new orders will be placed")

    def _on_signal(self, signal) -> None:
        self.live_page.add_signal(signal)
        self.set_status(f"Signal: {signal.asset} {signal.side} at {signal.entry_at:%H:%M:%S} UTC")
        self._refresh_stats()

    def _on_cancel(self, signal, reason: str) -> None:
        self.set_status(f"Cancelled {signal.asset}: {reason}")
        self.live_page.refresh()

    def _on_result(self, result) -> None:
        self.set_status(f"{result.result_word}: {result.signal.asset} ({result.pnl:+.2f})")
        self._refresh_stats()
        # History and Analytics rebuild when their tab is opened rather than on
        # every settled trade; redrawing a 500-row table per result is wasted
        # work while the user is looking at Live Signals.
        self._history_stale = True

    def _tick_ui(self) -> None:
        self.clock.setText(f"{datetime.now(timezone.utc):%H:%M:%S} UTC")
        self.live_page.refresh()
        if self.stack.currentIndex() == 3:
            self.diagnostics_page.refresh()

    def _refresh_stats(self) -> None:
        # All aggregates come from SQL: constant work regardless of history size.
        st = self.journal.stats()
        be = analytics.breakeven_win_rate(PAYOUT)

        self.live_page.stat_pending.set_value(str(st["pending"]))
        today = datetime.now(timezone.utc).date().isoformat()
        self.live_page.stat_today.set_value(str(self.journal.count_signals_since(today)))

        if st["trades"]:
            colour = theme.BUY if st["win_rate"] >= be else theme.SELL
            self.live_page.stat_winrate.set_value(
                f"{st['win_rate']:.1%}",
                f"break-even {be:.1%} · {int(st['trades'])} trades",
                colour,
            )
            self.live_page.stat_pnl.set_value(
                f"{st['pnl']:+.2f}", accent=theme.BUY if st["pnl"] >= 0 else theme.SELL
            )
        else:
            self.live_page.stat_winrate.set_value("--", f"break-even {be:.1%}")

    def set_status(self, text: str) -> None:
        """Show a message, elided to fit, with the full text always reachable.

        The status bar is one line wide; a broker error can name every asset
        on the account. Truncating without a way back to the rest is how a
        fixable problem becomes invisible, so the full text lives in the
        tooltip, behind the Details button, and in Diagnostics.
        """
        self.status_label.setToolTip(text)
        self._status_text = text
        self._elide_status()

    def _elide_status(self) -> None:
        metrics = self.status_label.fontMetrics()
        width = max(self.status_label.width(), 80)
        self.status_label.setText(
            metrics.elidedText(self._status_text, Qt.TextElideMode.ElideRight, width)
        )

    def show_error(self, text: str) -> None:
        self._last_error = text
        self.details_btn.setVisible(True)
        first = text.splitlines()[0] if text.splitlines() else text
        self.set_status(f"Error: {first}")
        # set_status tooltips the line it was given; an error's tooltip should
        # be the whole thing, so hovering is enough for most of them.
        self.status_label.setToolTip(text)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide_status()

    def closeEvent(self, event) -> None:
        if self.engine:
            self.engine.stop()
        self.journal.close()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_LONG_NAME)
    app.setWindowIcon(app_icon())
    app.setStyleSheet(theme.stylesheet())
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
