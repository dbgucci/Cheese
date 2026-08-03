"""Main window: Live signals, History, Analytics, Settings."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QTimer, Signal as QtSignal
from PySide6.QtGui import QDesktopServices, QFont, QIcon
from PySide6.QtCore import QUrl
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
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import analytics, paths, profiles, storage
from ..engine import SignalEngine
from ..settings import DEFAULT_ASSETS, Settings
from . import theme
from .widgets import EmptyState, SignalCard, StatCard, StatusDot

PAYOUT = 0.85

# Row caps. The journal is meant to grow for years; the views are not meant to
# read all of it. History shows a page, Analytics analyses a recent window.
HISTORY_ROWS = 500
ANALYTICS_ROWS = 20_000

# Vertical chrome of a _card() with a title: top+bottom margins (16+16),
# the section-title row, and the layout spacing between title and content.
_CARD_CHROME_H = 16 + 16 + 20 + 11


def _title_block(title: str, subtitle: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(3)
    t = QLabel(title)
    t.setObjectName("PageTitle")
    s = QLabel(subtitle)
    s.setObjectName("PageSubtitle")
    s.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


def _card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(11)
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

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 20)
        root.setSpacing(18)

        header = QHBoxLayout()
        header.addWidget(
            _title_block(
                "Live Signals",
                "Signals are announced ahead of their entry minute and re-checked on every "
                "candle until then.",
            )
        )
        header.addStretch(1)

        self.halt_btn = QPushButton("Halt Trading")
        self.halt_btn.setObjectName("Danger")
        self.halt_btn.setToolTip("Immediately stop placing new orders. Signals keep running.")
        self.halt_btn.clicked.connect(self.window.toggle_halt)
        self.halt_btn.setVisible(False)
        header.addWidget(self.halt_btn)

        self.start_btn = QPushButton("Start Engine")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(self.window.toggle_engine)
        header.addWidget(self.start_btn)
        root.addLayout(header)

        stats = QHBoxLayout()
        stats.setSpacing(13)
        self.stat_pending = StatCard("Awaiting entry", "0")
        self.stat_today = StatCard("Signals today", "0")
        self.stat_winrate = StatCard("Win rate", "--", f"break-even {1 / (1 + PAYOUT):.1%}")
        self.stat_pnl = StatCard("Net P/L", "0.00")
        for s in (self.stat_pending, self.stat_today, self.stat_winrate, self.stat_pnl):
            stats.addWidget(s)
        root.addLayout(stats)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.cards_layout = QVBoxLayout(holder)
        self.cards_layout.setContentsMargins(0, 0, 6, 0)
        self.cards_layout.setSpacing(11)

        self.empty = EmptyState(
            "No active signals",
            "Start the engine and the bot will watch your OTC pairs. When a setup forms you'll "
            "get the pair, direction, and the exact minute to enter — here and on Telegram.",
        )
        self.cards_layout.addWidget(self.empty)
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
    COLUMNS = ["Time (UTC)", "Pair", "Side", "Conf.", "Setup", "Result", "P/L", "Why"]

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 20)
        root.setSpacing(16)

        header = QHBoxLayout()
        header.addWidget(
            _title_block(
                "Trade History",
                "Every settled signal with the reason it won or lost. Stored permanently in your "
                "data folder.",
            )
        )
        header.addStretch(1)
        export = QPushButton("Export CSV")
        export.setObjectName("Ghost")
        export.clicked.connect(self.export_csv)
        folder = QPushButton("Open Data Folder")
        folder.setObjectName("Ghost")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.data_dir()))))
        header.addWidget(export)
        header.addWidget(folder)
        root.addLayout(header)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        hh = self.table.horizontalHeader()
        for i in range(len(self.COLUMNS) - 1):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(len(self.COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)

    def refresh(self) -> None:
        # Only fetch the page being displayed. Loading the whole journal to
        # render 500 rows is what made the app slow as history grew.
        rows = list(reversed(self.window.journal.joined_results(limit=HISTORY_ROWS)))
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            won = bool(row.get("won"))
            ts = str(row.get("entry_at", ""))[:19].replace("T", " ")
            values = [
                ts,
                str(row.get("asset", "")).replace("_otc", " OTC").upper(),
                "BUY" if row.get("direction") == 1 else "SELL",
                f"{float(row.get('score') or 0):.0%}",
                str(row.get("strategy", "")),
                "WIN" if won else "LOSS",
                f"{float(row.get('pnl') or 0):+.2f}",
                str(row.get("outcome_reason", "")),
            ]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 5:
                    item.setForeground(Qt.GlobalColor.green if won else Qt.GlobalColor.red)
                    f = QFont()
                    f.setBold(True)
                    item.setFont(f)
                self.table.setItem(r, c, item)

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

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 20)
        root.setSpacing(16)

        header = QHBoxLayout()
        header.addWidget(
            _title_block(
                "Analytics",
                "Realised performance from your own logged trades — sliced by the dimensions the "
                "engine can act on.",
            )
        )
        header.addStretch(1)

        self.filter_box = QComboBox()
        self.filter_box.addItems(analytics.FILTER_OPTIONS)
        self.filter_box.setMinimumWidth(150)
        self.filter_box.setToolTip(
            "Results from different builds are stored separately so a fix can be "
            "measured. 'This build only' hides trades produced by older versions."
        )
        self.filter_box.currentTextChanged.connect(lambda _: self.refresh())
        header.addWidget(self.filter_box)

        refresh = QPushButton("Refresh")
        refresh.setObjectName("Ghost")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        root.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.body = QVBoxLayout(holder)
        self.body.setContentsMargins(0, 0, 6, 0)
        self.body.setSpacing(14)
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
            table.verticalHeader().setDefaultSectionSize(32)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for i in range(1, 5):
                table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)

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
                        item.setForeground(Qt.GlobalColor.green if edge > 0 else Qt.GlobalColor.red)
                    table.setItem(r, c, item)

            # Header + rows + border, so the whole table is visible without
            # its own scrollbar.
            table_h = len(slices) * 32 + 44
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

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 20)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.addWidget(
            _title_block(
                "Diagnostics",
                "Every pair the engine reads, each condition it checks, and the reason a "
                "setup fired or did not. Written to a log file in your data folder too.",
            )
        )
        header.addStretch(1)

        self.only_fired = QCheckBox("Signals only")
        self.only_fired.stateChanged.connect(lambda _: self.refresh())
        header.addWidget(self.only_fired)

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setObjectName("Ghost")
        self.pause_btn.clicked.connect(self._toggle_pause)
        header.addWidget(self.pause_btn)

        clear = QPushButton("Clear")
        clear.setObjectName("Ghost")
        clear.clicked.connect(self._clear)
        header.addWidget(clear)

        logs = QPushButton("Open Log Folder")
        logs.setObjectName("Ghost")
        logs.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs_dir())))
        )
        header.addWidget(logs)
        root.addLayout(header)

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

    def refresh(self) -> None:
        eng = self.window.engine
        if eng is None:
            self.summary.setText(
                "Engine not running. Start it from Live Signals and this will fill in."
            )
            return
        if self.paused:
            return

        traces = eng.traces.recent(limit=250, only_fired=self.only_fired.isChecked())
        counts = eng.traces.counts()
        if counts:
            parts = [f"{v} {k.lower()}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
            self.summary.setText("Recent activity: " + ", ".join(parts))
        else:
            self.summary.setText("Waiting for the first candle...")

        # Preserve the scroll position unless the user is pinned to the bottom.
        bar = self.view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.view.setPlainText("\n".join(t.as_text() for t in traces))
        if at_bottom:
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())


class SettingsPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        s = window.settings

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 20)
        root.setSpacing(16)
        root.addWidget(
            _title_block("Settings", "Changes are saved to your data folder and applied on the next engine start.")
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        body = QVBoxLayout(holder)
        body.setContentsMargins(0, 0, 6, 0)
        body.setSpacing(14)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

        # ---------------- strategy ----------------
        card, lay = _card("Strategy")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(11)

        self.strategy_box = QComboBox()
        self.strategy_box.addItems(["trend_continuation", "liquidity_sweep"])
        self.strategy_box.setCurrentText(s.strategy)
        grid.addWidget(QLabel("Setup to trade"), 0, 0)
        grid.addWidget(self.strategy_box, 0, 1)
        grid.setColumnStretch(0, 1)
        lay.addLayout(grid)

        strat_note = QLabel(
            "trend_continuation trades WITH the trend: Heikin Ashi above/below the Keltner "
            "mid (EMA 20), price on the same side of the EMA 200, triggered by a confirmed "
            "period-7 fractal. liquidity_sweep trades reversals — the opposite posture. "
            "Only one runs at a time.\n\n"
            "Run 'cheese-signals lab' to compare both on your own recorded candles before "
            "choosing."
        )
        strat_note.setObjectName("Hint")
        strat_note.setWordWrap(True)
        lay.addWidget(strat_note)

        self.adaptive_expiry = QCheckBox(
            "Choose expiry automatically from how long moves have been lasting"
        )
        self.adaptive_expiry.setChecked(s.adaptive_expiry)
        lay.addWidget(self.adaptive_expiry)

        grid2 = QGridLayout()
        grid2.setHorizontalSpacing(16)
        grid2.setVerticalSpacing(11)
        self.expiry_min = QSpinBox()
        self.expiry_min.setRange(1, 10)
        self.expiry_min.setValue(s.expiry_min_minutes)
        self.expiry_min.setSuffix("  min")
        self.expiry_max = QSpinBox()
        self.expiry_max.setRange(1, 15)
        self.expiry_max.setValue(s.expiry_max_minutes)
        self.expiry_max.setSuffix("  min")
        grid2.addWidget(QLabel("Shortest expiry it may pick"), 0, 0)
        grid2.addWidget(self.expiry_min, 0, 1)
        grid2.addWidget(QLabel("Longest expiry it may pick"), 1, 0)
        grid2.addWidget(self.expiry_max, 1, 1)

        self.fractal_age = QSpinBox()
        self.fractal_age.setRange(0, 40)
        self.fractal_age.setValue(s.fractal_max_age)
        self.fractal_age.setSuffix("  candles")
        self.fractal_age.setToolTip(
            "How recently a fractal must have formed to trigger a trade. Too small and "
            "the strategy stays silent through long trends; too large and it enters on a "
            "pullback that already finished."
        )
        grid2.addWidget(QLabel("Fractal trigger window"), 2, 0)
        grid2.addWidget(self.fractal_age, 2, 1)
        grid2.setColumnStretch(0, 1)
        lay.addLayout(grid2)
        body.addWidget(card)

        # ---------------- signal timing ----------------
        card, lay = _card("Signal timing")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(11)

        self.lead = QSpinBox()
        self.lead.setRange(0, 10)
        self.lead.setValue(s.lead_minutes)
        self.lead.setSuffix("  min")
        grid.addWidget(QLabel("Advance warning before entry"), 0, 0)
        grid.addWidget(self.lead, 0, 1)

        self.expiry = QSpinBox()
        self.expiry.setRange(1, 15)
        self.expiry.setValue(s.expiry_minutes)
        self.expiry.setSuffix("  min")
        grid.addWidget(QLabel("Option expiry"), 1, 0)
        grid.addWidget(self.expiry, 1, 1)

        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 60)
        self.cooldown.setValue(s.cooldown_minutes)
        self.cooldown.setSuffix("  min")
        grid.addWidget(QLabel("Cooldown between signals per pair"), 2, 0)
        grid.addWidget(self.cooldown, 2, 1)
        grid.setColumnStretch(0, 1)
        lay.addLayout(grid)

        hint = QLabel(
            "A longer advance warning gives you more time to place the trade, but the market keeps "
            "moving in between, so the prediction is weaker. The signal is re-checked every candle "
            "and cancelled if the setup breaks down. Check the Analytics tab's "
            "'By advance-warning time' table to see what it actually costs you."
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        body.addWidget(card)

        # ---------------- signal quality ----------------
        card, lay = _card("Signal quality")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(11)

        self.min_score = QDoubleSpinBox()
        self.min_score.setRange(0.30, 1.0)
        self.min_score.setSingleStep(0.05)
        self.min_score.setValue(s.min_score)
        grid.addWidget(QLabel("Minimum confidence to fire"), 0, 0)
        grid.addWidget(self.min_score, 0, 1)
        grid.setColumnStretch(0, 1)
        lay.addLayout(grid)

        self.require_sweep = QCheckBox("Only trade liquidity-sweep setups (highest conviction, fewer signals)")
        self.require_sweep.setChecked(s.require_liquidity_sweep)
        self.use_bias = QCheckBox("Use higher-timeframe trend bias as a filter")
        self.use_bias.setChecked(s.use_higher_timeframe_bias)
        self.restrict_sessions = QCheckBox("Restrict trading to specific sessions")
        self.restrict_sessions.setChecked(s.restrict_to_sessions)
        for cb in (self.require_sweep, self.use_bias, self.restrict_sessions):
            lay.addWidget(cb)

        note = QLabel(
            "OTC pairs are broker-generated synthetic feeds that run 24/7, so 'London session' "
            "liquidity does not literally apply to them. Leave session restriction off until the "
            "Analytics tab shows a real per-session difference in your own results."
        )
        note.setObjectName("Hint")
        note.setWordWrap(True)
        lay.addWidget(note)
        body.addWidget(card)

        # ---------------- pairs ----------------
        card, lay = _card("Pairs to watch")
        self.assets_edit = QTextEdit()
        self.assets_edit.setPlainText("\n".join(s.assets))
        self.assets_edit.setMaximumHeight(120)
        lay.addWidget(self.assets_edit)
        h = QLabel("One per line. Pocket Option OTC symbols end in _otc (e.g. EURUSD_otc).")
        h.setObjectName("Hint")
        lay.addWidget(h)
        body.addWidget(card)

        # ---------------- autotrading ----------------
        card, lay = _card("Autotrading")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(11)

        self.trade_mode = QComboBox()
        self.trade_mode.addItems(["off", "paper", "live"])
        self.trade_mode.setCurrentText(getattr(s, "trade_mode", "off"))
        grid.addWidget(QLabel("Execution mode"), 0, 0)
        grid.addWidget(self.trade_mode, 0, 1)

        self.max_stake = QDoubleSpinBox()
        self.max_stake.setRange(1, 10000)
        self.max_stake.setValue(getattr(s, "max_stake", 50.0))
        self.max_stake.setPrefix("$ ")
        grid.addWidget(QLabel("Hard cap per trade"), 1, 0)
        grid.addWidget(self.max_stake, 1, 1)

        self.max_concurrent = QSpinBox()
        self.max_concurrent.setRange(1, 20)
        self.max_concurrent.setValue(getattr(s, "max_concurrent_trades", 3))
        grid.addWidget(QLabel("Max trades open at once"), 2, 0)
        grid.addWidget(self.max_concurrent, 2, 1)

        self.max_daily_loss = QDoubleSpinBox()
        self.max_daily_loss.setRange(1, 100000)
        self.max_daily_loss.setValue(getattr(s, "max_daily_loss", 100.0))
        self.max_daily_loss.setPrefix("$ ")
        grid.addWidget(QLabel("Stop trading after losing"), 3, 0)
        grid.addWidget(self.max_daily_loss, 3, 1)

        self.min_balance = QDoubleSpinBox()
        self.min_balance.setRange(0, 100000)
        self.min_balance.setValue(getattr(s, "min_balance", 50.0))
        self.min_balance.setPrefix("$ ")
        grid.addWidget(QLabel("Never trade below balance"), 4, 0)
        grid.addWidget(self.min_balance, 4, 1)
        grid.setColumnStretch(0, 1)
        lay.addLayout(grid)

        trade_note = QLabel(
            "off — signals only, nothing is placed.\n"
            "paper — simulated trades against the real feed with a simulated balance. "
            "This is how you find out whether the strategy is worth trading, at no risk.\n"
            "live — places REAL orders with REAL money on your Pocket Option account.\n\n"
            "Every order is checked against the caps above first, and trading halts "
            "automatically when the daily loss limit is reached. Do not switch to live "
            "until the Analytics tab shows an edge over a meaningful number of trades."
        )
        trade_note.setObjectName("Hint")
        trade_note.setWordWrap(True)
        lay.addWidget(trade_note)
        body.addWidget(card)

        # ---------------- risk ----------------
        card, lay = _card("Risk")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(11)

        self.balance = QDoubleSpinBox()
        self.balance.setRange(10, 1_000_000)
        self.balance.setValue(s.account_balance)
        self.balance.setPrefix("$ ")
        grid.addWidget(QLabel("Account balance"), 0, 0)
        grid.addWidget(self.balance, 0, 1)

        self.risk_pct = QDoubleSpinBox()
        self.risk_pct.setRange(0.1, 20.0)
        self.risk_pct.setSingleStep(0.5)
        self.risk_pct.setValue(s.risk_per_trade * 100)
        self.risk_pct.setSuffix(" %")
        grid.addWidget(QLabel("Risk per trade"), 1, 0)
        grid.addWidget(self.risk_pct, 1, 1)

        self.max_trades = QSpinBox()
        self.max_trades.setRange(1, 60)
        self.max_trades.setValue(s.max_trades_per_hour)
        grid.addWidget(QLabel("Max trades per hour"), 2, 0)
        grid.addWidget(self.max_trades, 2, 1)

        self.daily_loss = QDoubleSpinBox()
        self.daily_loss.setRange(1.0, 50.0)
        self.daily_loss.setValue(s.max_daily_loss_fraction * 100)
        self.daily_loss.setSuffix(" %")
        grid.addWidget(QLabel("Stop for the day after losing"), 3, 0)
        grid.addWidget(self.daily_loss, 3, 1)
        grid.setColumnStretch(0, 1)
        lay.addLayout(grid)
        body.addWidget(card)

        # ---------------- connection ----------------
        card, lay = _card("Data source")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(11)

        self.source = QComboBox()
        self.source.addItems(["synthetic", "pocket_option"])
        self.source.setCurrentText(s.data_source)
        grid.addWidget(QLabel("Feed"), 0, 0)
        grid.addWidget(self.source, 0, 1)

        self.ssid = QLineEdit(s.pocket_option_ssid)
        self.ssid.setEchoMode(QLineEdit.EchoMode.Password)
        self.ssid.setPlaceholderText("Pocket Option session ID")
        grid.addWidget(QLabel("Pocket Option SSID"), 1, 0)
        grid.addWidget(self.ssid, 1, 1)
        grid.setColumnStretch(0, 1)
        lay.addLayout(grid)

        h = QLabel(
            "'synthetic' generates practice data locally and needs no credentials — use it to try "
            "the app safely. 'pocket_option' requires the optional binaryoptionstoolsv2 package."
        )
        h.setObjectName("Hint")
        h.setWordWrap(True)
        lay.addWidget(h)
        body.addWidget(card)

        # ---------------- telegram ----------------
        card, lay = _card("Telegram notifications")
        self.tg_enabled = QCheckBox("Send signal alerts and win/loss results to Telegram")
        self.tg_enabled.setChecked(s.telegram_enabled)
        lay.addWidget(self.tg_enabled)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(11)
        self.tg_token = QLineEdit(s.telegram_bot_token)
        self.tg_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.tg_token.setPlaceholderText("123456:ABC-DEF...")
        self.tg_chat = QLineEdit(s.telegram_chat_id)
        self.tg_chat.setPlaceholderText("your chat ID")
        grid.addWidget(QLabel("Bot token"), 0, 0)
        grid.addWidget(self.tg_token, 0, 1)
        grid.addWidget(QLabel("Chat ID"), 1, 0)
        grid.addWidget(self.tg_chat, 1, 1)
        grid.setColumnStretch(0, 1)
        lay.addLayout(grid)

        test = QPushButton("Send test message")
        test.setObjectName("Ghost")
        test.clicked.connect(self.test_telegram)
        lay.addWidget(test, 0, Qt.AlignmentFlag.AlignLeft)
        body.addWidget(card)

        body.addStretch(1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        save = QPushButton("Save Settings")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        footer.addWidget(save)
        root.addLayout(footer)

    def test_telegram(self) -> None:
        from ..notifiers import TelegramNotifier

        token, chat = self.tg_token.text().strip(), self.tg_chat.text().strip()
        if not token or not chat:
            QMessageBox.warning(self, "Telegram", "Enter both a bot token and a chat ID first.")
            return
        ok, msg = TelegramNotifier(token, chat).test()
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Telegram", msg)

    def save(self) -> None:
        mode = self.trade_mode.currentText()
        confirmed = getattr(self.window.settings, "live_confirmed", False)
        if mode == "live" and not confirmed:
            # Real money needs a deliberate act, not a dropdown selection.
            typed, ok = QInputDialog.getText(
                self, "Confirm live trading",
                "This will place REAL orders with REAL money on your Pocket Option\n"
                "account, automatically, without asking again.\n\n"
                "Your logged results do not yet show a proven edge.\n\n"
                'Type  TRADE LIVE  to confirm, or Cancel to stay in paper mode:',
            )
            if not ok or typed.strip() != "TRADE LIVE":
                self.trade_mode.setCurrentText("paper")
                mode = "paper"
                QMessageBox.information(
                    self, "Autotrading",
                    "Not confirmed — left in paper mode.",
                )
            else:
                confirmed = True
        if mode != "live":
            confirmed = False

        assets = [a.strip() for a in self.assets_edit.toPlainText().splitlines() if a.strip()]
        self.window.settings.update(
            trade_mode=mode,
            live_confirmed=confirmed,
            strategy=self.strategy_box.currentText(),
            adaptive_expiry=self.adaptive_expiry.isChecked(),
            expiry_min_minutes=self.expiry_min.value(),
            expiry_max_minutes=max(self.expiry_max.value(), self.expiry_min.value()),
            fractal_max_age=self.fractal_age.value(),
            max_stake=self.max_stake.value(),
            max_concurrent_trades=self.max_concurrent.value(),
            max_daily_loss=self.max_daily_loss.value(),
            min_balance=self.min_balance.value(),
            lead_minutes=self.lead.value(),
            expiry_minutes=self.expiry.value(),
            cooldown_minutes=self.cooldown.value(),
            min_score=self.min_score.value(),
            require_liquidity_sweep=self.require_sweep.isChecked(),
            use_higher_timeframe_bias=self.use_bias.isChecked(),
            restrict_to_sessions=self.restrict_sessions.isChecked(),
            assets=assets or list(DEFAULT_ASSETS),
            account_balance=self.balance.value(),
            risk_per_trade=self.risk_pct.value() / 100.0,
            max_trades_per_hour=self.max_trades.value(),
            max_daily_loss_fraction=self.daily_loss.value() / 100.0,
            data_source=self.source.currentText(),
            pocket_option_ssid=self.ssid.text().strip(),
            telegram_enabled=self.tg_enabled.isChecked(),
            telegram_bot_token=self.tg_token.text().strip(),
            telegram_chat_id=self.tg_chat.text().strip(),
        )
        self.window.set_status("Settings saved. Restart the engine to apply.")
        QMessageBox.information(self, "Settings", "Saved to your data folder.")


class MainWindow(QMainWindow):
    sig_signal = QtSignal(object)
    sig_cancel = QtSignal(object, str)
    sig_result = QtSignal(object)
    sig_status = QtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cheese Signals")
        self.resize(1240, 830)
        self.setMinimumSize(1060, 700)

        self.settings = Settings.load()
        self.journal = storage.Journal()
        self._history_stale = False
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
        bar.setFixedWidth(214)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(15, 22, 15, 18)
        lay.setSpacing(6)

        brand = QLabel("Cheese Signals")
        brand.setObjectName("BrandMark")
        sub = QLabel("OTC · 1-Minute")
        sub.setObjectName("BrandSub")
        lay.addWidget(brand)
        lay.addWidget(sub)
        lay.addSpacing(22)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for i, (label, _) in enumerate(
            [("Live Signals", 0), ("History", 1), ("Analytics", 2),
             ("Diagnostics", 3), ("Settings", 4)]
        ):
            btn = QPushButton(f"   {label}")
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setChecked(i == 0)
            btn.clicked.connect(lambda _=False, idx=i: self._navigate(idx))
            self.nav_group.addButton(btn, i)
            lay.addWidget(btn)

        lay.addStretch(1)

        self.sidebar_note = QLabel("Engine stopped")
        self.sidebar_note.setObjectName("Hint")
        self.sidebar_note.setWordWrap(True)
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
        lay.addWidget(self.status_dot)
        lay.addWidget(self.status_label)
        lay.addStretch(1)

        self.clock = QLabel()
        self.clock.setObjectName("StatusText")
        lay.addWidget(self.clock)
        return bar

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
                on_error=lambda m: self.sig_status.emit(f"Error: {m.splitlines()[0]}"),
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
        self.status_label.setText(text)

    def closeEvent(self, event) -> None:
        if self.engine:
            self.engine.stop()
        self.journal.close()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Cheese Signals")
    app.setStyleSheet(theme.stylesheet())
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
