"""ORB Autobot: the desktop window.

Same design system as the signals app next to it -- ``theme.py`` is imported,
not reimplemented, so a change to the palette moves both. What differs is what
the window is *for*: this one supervises a bot that is placing real orders, so
the layout answers a different set of questions.

Three of those questions drove the layout:

* **Which account am I about to trade?** Named on the dashboard, in full,
  before anything else. Two brokers' terminals open at once is a normal
  situation and picking the wrong one is silent.
* **Why is it not trading right now?** Almost always the interesting question,
  and almost always unanswered by trading software. Every instrument has a
  status and a reason on the dashboard, and the full trace is one click away.
* **Am I live?** The window says so continuously, in the title bar, in the
  status bar, and by turning the start control red. A dry run that is mistaken
  for live is a wasted afternoon; live mistaken for a dry run is an account.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..markets.execution import BUY
from ..markets.launcher import LauncherSettings, settings_path
from . import theme
from .autobot_worker import BACKTEST, CONNECT, START, STOP, SURVEY, BotWorker, Snapshot
from .branding import app_icon
from .icons import icon as nav_icon
from .widgets import EmptyState, StatStrip, StatTile, StatusDot, hairline

APP_NAME = "ORB Autobot"
APP_TAGLINE = "FX · METALS · INDICES"
MARK = "ORB"

# The log is a trace, not a journal: it is bounded so a bot left running for a
# week does not turn the window into a memory leak.
LOG_ROWS = 2_000

EVENT_COLOURS = {
    "opened": theme.GOLD,
    "closed": theme.TEXT,
    "settled": theme.TEXT,
    "trailed": theme.GOLD_DIM,
    "blocked": theme.WARN,
    "warning": theme.WARN,
    "error": theme.SELL,
    "skipped": theme.TEXT_FAINT,
    "clock": theme.TEXT_MUTED,
}


# --------------------------------------------------------------------------
# small shared builders, mirroring app.py's so the two windows match
# --------------------------------------------------------------------------
def _page_layout(widget: QWidget) -> QVBoxLayout:
    lay = QVBoxLayout(widget)
    lay.setContentsMargins(theme.PAGE_MARGIN_H, theme.PAGE_MARGIN_TOP,
                           theme.PAGE_MARGIN_H, theme.PAGE_MARGIN_BOTTOM)
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
    s.setMinimumHeight(38)
    s.setAlignment(Qt.AlignmentFlag.AlignTop)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


def _page_header(title: str, subtitle: str, actions: list[QWidget]) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(10)
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


def _table(headers: list[str], stretch: int = 0) -> QTableWidget:
    """A fixed-metric table.

    Column sizing is never ``ResizeToContents``: doing that on a populated,
    visible table re-measures every cell on every write, which is the bug that
    once took the History tab of the sibling app to 90 seconds to open.
    """
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(34)
    t.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setShowGrid(False)
    header = t.horizontalHeader()
    for i in range(len(headers)):
        mode = (QHeaderView.ResizeMode.Stretch if i == stretch
                else QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(i, mode)
        if i != stretch:
            t.setColumnWidth(i, 110)
    return t


def _cell(text: str, colour: str | None = None, mono: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if colour:
        item.setForeground(QColor(colour))
    if mono:
        font = item.font()
        font.setFamily("Consolas")
        item.setFont(font)
    return item


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------
class DashboardPage(QWidget):
    def __init__(self, window: "AutobotWindow"):
        super().__init__()
        self.window = window
        root = _page_layout(self)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("Ghost")
        self.connect_btn.clicked.connect(window.do_connect)

        self.live_toggle = QCheckBox("Trade live")
        self.live_toggle.setToolTip(
            "Off: the bot decides and reports, but sends nothing to the broker.")
        self.live_toggle.toggled.connect(window.on_live_toggled)

        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("Primary")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(window.do_start_stop)

        root.addLayout(_page_header(
            "Dashboard",
            "The account being traded, what each instrument is doing, and why "
            "it is or is not in a trade.",
            [self.connect_btn, self.live_toggle, self.start_btn]))

        self.tile_equity = StatTile("Equity")
        self.tile_pnl = StatTile("Today")
        self.tile_trades = StatTile("Trades today")
        self.tile_open = StatTile("Open positions")
        root.addWidget(StatStrip([self.tile_equity, self.tile_pnl,
                                  self.tile_trades, self.tile_open]))

        self.broker_card, broker_lay = _card("Broker")
        self.broker_text = QLabel("Not connected.")
        self.broker_text.setObjectName("Hint")
        self.broker_text.setWordWrap(True)
        self.broker_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        broker_lay.addWidget(self.broker_text)
        root.addWidget(self.broker_card)

        sessions_card, sessions_lay = _card("Today's sessions")
        self.sessions_table = _table(
            ["Instrument", "Session", "Range (UTC)", "Flat by", "Range", "Status",
             "Why"], stretch=6)
        self.sessions_table.setColumnWidth(0, 120)
        self.sessions_table.setColumnWidth(1, 160)
        self.sessions_table.setMinimumHeight(200)
        sessions_lay.addWidget(self.sessions_table)
        root.addWidget(sessions_card, 1)

        positions_card, positions_lay = _card("Open positions")
        self.positions_table = _table(
            ["Instrument", "Side", "Lots", "Entry", "Stop", "Target", "P/L",
             "Ticket"], stretch=0)
        self.positions_table.setMaximumHeight(180)
        positions_lay.addWidget(self.positions_table)
        root.addWidget(positions_card)

    # ---------------------------------------------------------------- update
    def set_broker(self, diag: dict) -> None:
        algo = ("algo trading enabled" if diag["algo_allowed"]
                else 'ALGO TRADING OFF — tick "Algo Trading" in the MT5 toolbar')
        experts = ("" if diag["trade_expert"]
                   else "  ·  the broker has disabled automated trading on this account")
        self.broker_text.setText(
            f"{diag['company']}  ·  server {diag['server']}  ·  account "
            f"{diag['login']} ({diag['currency']})\n{algo}{experts}\n"
            f"{diag['terminal_path']}")
        self.broker_text.setObjectName(
            "Hint" if diag["algo_allowed"] and diag["trade_expert"] else "WarnText")
        self.broker_text.setStyleSheet(
            "" if diag["algo_allowed"] and diag["trade_expert"]
            else f"color: {theme.WARN};")

    def set_error(self, error: str, help_lines: list[str]) -> None:
        self.broker_text.setStyleSheet(f"color: {theme.WARN};")
        self.broker_text.setText("\n".join(help_lines))

    def update_snapshot(self, snap: Snapshot) -> None:
        self.tile_equity.set_value(f"{snap.equity:,.2f}",
                                   f"balance {snap.balance:,.2f}")
        accent = (theme.BUY if snap.day_pnl > 0
                  else theme.SELL if snap.day_pnl < 0 else None)
        self.tile_pnl.set_value(f"{snap.day_pnl:+,.2f}",
                                f"{snap.day_pnl_fraction:+.2%}", accent)
        self.tile_trades.set_value(str(snap.trades_today),
                                   "halted" if snap.halted else "")
        self.tile_open.set_value(str(len(snap.open_positions)))

        self.sessions_table.setRowCount(len(snap.sessions))
        for r, row in enumerate(snap.sessions):
            width = "--" if row.width_points is None else f"{row.width_points:.0f}pt"
            colour = (theme.GOLD if row.status == "in a trade"
                      else theme.TEXT_FAINT if row.status in ("closed", "not traded")
                      else theme.TEXT)
            for c, value in enumerate([row.symbol, row.session, row.range_window,
                                       row.flat_by, width, row.status, row.detail]):
                self.sessions_table.setItem(
                    r, c, _cell(value, colour if c in (0, 5) else None,
                                mono=c in (2, 3, 4)))

        self.positions_table.setRowCount(len(snap.open_positions))
        for r, p in enumerate(snap.open_positions):
            side = "BUY" if p["direction"] == BUY else "SELL"
            side_colour = theme.BUY if p["direction"] == BUY else theme.SELL
            pnl_colour = (theme.BUY if p["profit"] > 0
                          else theme.SELL if p["profit"] < 0 else None)
            values = [p["symbol"], side, f"{p['lots']:g}", f"{p['entry']:.5f}",
                      f"{p['stop']:.5f}",
                      f"{p['target']:.5f}" if p["target"] else "--",
                      f"{p['profit']:+,.2f}", str(p["ticket"])]
            for c, value in enumerate(values):
                colour = (side_colour if c == 1 else pnl_colour if c == 6 else None)
                self.positions_table.setItem(r, c, _cell(value, colour, mono=c >= 2))


# --------------------------------------------------------------------------
# backtest
# --------------------------------------------------------------------------
class BacktestPage(QWidget):
    def __init__(self, window: "AutobotWindow"):
        super().__init__()
        self.window = window
        root = _page_layout(self)

        self.days = QSpinBox()
        self.days.setRange(20, 2000)
        self.days.setValue(window.settings.backtest_days)
        self.days.setSuffix(" days")
        self.days.setFixedWidth(120)

        self.survey_btn = QPushButton("Cost wall")
        self.survey_btn.setObjectName("Ghost")
        self.survey_btn.clicked.connect(
            lambda: window.worker.post(SURVEY, days=90))

        self.run_btn = QPushButton("Run backtest")
        self.run_btn.setObjectName("Primary")
        self.run_btn.clicked.connect(
            lambda: window.worker.post(BACKTEST, days=self.days.value()))

        root.addLayout(_page_header(
            "Backtest",
            "Walks the rules forward over this broker's own history and its own "
            "quoted spreads. Read the comparison table before the headline: a "
            "strategy that cannot beat its own mirror has found drift, not an edge.",
            [self.days, self.survey_btn, self.run_btn]))

        compare_card, compare_lay = _card("Comparison")
        self.compare_table = _table(
            ["Run", "Trades", "Win rate", "R / trade", "Total R", "Max DD",
             "What it is"], stretch=6)
        self.compare_table.setMaximumHeight(190)
        compare_lay.addWidget(self.compare_table)
        root.addWidget(compare_card)

        detail_card, detail_lay = _card("Detail")
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText(
            "No backtest run yet. Connect, then press Run backtest.")
        self.output.setStyleSheet(
            f"font-family: {theme.MONO_STACK}; font-size: 12px; "
            f"background: {theme.BG_ELEVATED}; border: none;")
        detail_lay.addWidget(self.output)
        root.addWidget(detail_card, 1)

    def set_results(self, results: dict, text: str) -> None:
        labels = {"strategy": "as configured", "inverted": "every signal reversed",
                  "zero_cost": "no spread or commission",
                  "no_filters": "range/cost filters off"}
        order = ["strategy", "inverted", "zero_cost", "no_filters"]
        self.compare_table.setRowCount(len(order))
        for r, key in enumerate(order):
            rep = results.get(key)
            if rep is None:
                continue
            accent = (theme.BUY if rep.expectancy_r > 0
                      else theme.SELL if rep.expectancy_r < 0 else None)
            values = [key, str(rep.count), f"{rep.win_rate:.1%}",
                      f"{rep.expectancy_r:+.3f}", f"{rep.net_r:+.1f}",
                      f"{rep.max_drawdown_r:.1f}", labels[key]]
            for c, value in enumerate(values):
                self.compare_table.setItem(
                    r, c, _cell(value, accent if c in (3, 4) else None,
                                mono=1 <= c <= 5))
        self.output.setPlainText(text)

    def set_text(self, text: str) -> None:
        self.output.setPlainText(text)


# --------------------------------------------------------------------------
# activity log
# --------------------------------------------------------------------------
class LogPage(QWidget):
    def __init__(self, window: "AutobotWindow"):
        super().__init__()
        self.window = window
        self._paused = False
        root = _page_layout(self)

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setObjectName("Ghost")
        self.pause_btn.clicked.connect(self._toggle)

        clear = QPushButton("Clear")
        clear.setObjectName("Ghost")
        clear.clicked.connect(lambda: self.table.setRowCount(0))

        folder = QPushButton("Open log folder")
        folder.setObjectName("Ghost")
        folder.clicked.connect(window.open_data_folder)

        root.addLayout(_page_header(
            "Activity",
            "Everything the bot did and everything it refused to do, with the "
            "reason attached. A bot that does not trade and does not say why is "
            "indistinguishable from one that has been broken for a fortnight.",
            [self.pause_btn, clear, folder]))

        card, lay = _card()
        self.table = _table(["Time", "Instrument", "Event", "Detail"], stretch=3)
        self.table.setColumnWidth(0, 90)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 100)
        lay.addWidget(self.table)
        root.addWidget(card, 1)

    def _toggle(self) -> None:
        self._paused = not self._paused
        self.pause_btn.setText("Resume" if self._paused else "Pause")

    def add(self, event) -> None:
        if self._paused:
            return
        row = 0
        self.table.insertRow(0)        # newest first: the interesting end
        colour = EVENT_COLOURS.get(event.kind, theme.TEXT)
        for c, value in enumerate([f"{event.at:%H:%M:%S}", event.symbol,
                                   event.kind, event.detail]):
            self.table.setItem(row, c, _cell(value, colour, mono=c == 0))
        while self.table.rowCount() > LOG_ROWS:
            self.table.removeRow(self.table.rowCount() - 1)


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
class SettingsPage(QWidget):
    def __init__(self, window: "AutobotWindow"):
        super().__init__()
        self.window = window
        root = _page_layout(self)

        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)

        reveal = QPushButton("Open settings folder")
        reveal.setObjectName("Ghost")
        reveal.clicked.connect(window.open_data_folder)

        root.addLayout(_page_header(
            "Settings",
            f"Saved to {settings_path()}. Changes apply the next time you press "
            f"Start; a running bot keeps the settings it started with.",
            [reveal, save]))

        s = window.settings

        conn_card, conn_lay = _card("Connection")
        self.terminal = QLineEdit(s.terminal_path or "")
        self.terminal.setPlaceholderText(
            "Leave empty to attach to whichever MetaTrader 5 is running")
        browse = QPushButton("Browse...")
        browse.setObjectName("Ghost")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.terminal, 1)
        row.addWidget(browse)
        conn_lay.addLayout(self._labelled("MetaTrader 5 terminal", row))

        self.login = QLineEdit("" if s.login is None else str(s.login))
        self.login.setPlaceholderText("Only needed if the terminal is not logged in")
        conn_lay.addLayout(self._labelled("Account login", self.login))
        self.server = QLineEdit(s.server)
        self.server.setPlaceholderText("e.g. YourBroker-Live")
        conn_lay.addLayout(self._labelled("Server", self.server))
        root.addWidget(conn_card)

        strat_card, strat_lay = _card("Strategy")
        self.range_minutes = QSpinBox()
        self.range_minutes.setRange(1, 240)
        self.range_minutes.setValue(s.range_minutes)
        self.range_minutes.setSuffix(" min")
        strat_lay.addLayout(self._labelled(
            "Opening range length (indices use this; FX and metals get 30)",
            self.range_minutes))

        self.target_r = QDoubleSpinBox()
        self.target_r.setRange(0.25, 10.0)
        self.target_r.setSingleStep(0.25)
        self.target_r.setValue(s.target_r)
        self.target_r.setSuffix(" R")
        strat_lay.addLayout(self._labelled("Take profit", self.target_r))

        self.breakeven = QComboBox()
        self.breakeven.addItems(["Off", "1.0 R", "1.5 R"])
        self.breakeven.setCurrentIndex(
            0 if s.breakeven_at_r is None else 1 if s.breakeven_at_r <= 1.0 else 2)
        strat_lay.addLayout(self._labelled("Move the stop to entry at", self.breakeven))

        self.max_trades = QSpinBox()
        self.max_trades.setRange(1, 5)
        self.max_trades.setValue(s.max_trades_per_session)
        strat_lay.addLayout(self._labelled(
            "Trades per session, per instrument", self.max_trades))
        root.addWidget(strat_card)

        risk_card, risk_lay = _card("Risk")
        self.risk = QDoubleSpinBox()
        self.risk.setRange(0.01, 5.0)
        self.risk.setSingleStep(0.05)
        self.risk.setDecimals(2)
        self.risk.setValue(s.risk_fraction * 100)
        self.risk.setSuffix(" % of equity per trade")
        risk_lay.addLayout(self._labelled("Risk per trade", self.risk))

        self.daily_loss = QDoubleSpinBox()
        self.daily_loss.setRange(0.1, 50.0)
        self.daily_loss.setSingleStep(0.5)
        self.daily_loss.setValue(s.max_daily_loss_fraction * 100)
        self.daily_loss.setSuffix(" %")
        risk_lay.addLayout(self._labelled(
            "Stop trading for the day after losing", self.daily_loss))

        self.max_open = QSpinBox()
        self.max_open.setRange(1, 20)
        self.max_open.setValue(s.max_open_positions)
        risk_lay.addLayout(self._labelled("Most positions open at once", self.max_open))
        root.addWidget(risk_card)

        sym_card, sym_lay = _card("Instruments")
        self.symbols = QTextEdit()
        self.symbols.setPlainText("\n".join(s.symbols))
        self.symbols.setFixedHeight(140)
        self.symbols.setStyleSheet(f"font-family: {theme.MONO_STACK};")
        sym_lay.addWidget(QLabel(
            "One per line. Your broker's suffixes are matched automatically, so "
            "XAUUSD finds XAUUSD.r."))
        sym_lay.addWidget(self.symbols)
        root.addWidget(sym_card)
        root.addStretch(1)

    @staticmethod
    def _labelled(text: str, widget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        label = QLabel(text)
        label.setObjectName("Hint")
        label.setMinimumWidth(320)
        label.setWordWrap(True)
        row.addWidget(label, 1)
        if isinstance(widget, QHBoxLayout):
            row.addLayout(widget, 1)
        else:
            widget.setFixedWidth(260)
            row.addWidget(widget, 0)
        return row

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Find terminal64.exe", "", "MetaTrader (terminal64.exe terminal.exe)")
        if path:
            self.terminal.setText(path)

    def save(self) -> None:
        s = self.window.settings
        s.terminal_path = self.terminal.text().strip() or None
        text = self.login.text().strip()
        s.login = int(text) if text.isdigit() else None
        s.server = self.server.text().strip()
        s.range_minutes = self.range_minutes.value()
        s.target_r = self.target_r.value()
        s.breakeven_at_r = [None, 1.0, 1.5][self.breakeven.currentIndex()]
        s.max_trades_per_session = self.max_trades.value()
        s.risk_fraction = self.risk.value() / 100.0
        s.max_daily_loss_fraction = self.daily_loss.value() / 100.0
        s.max_open_positions = self.max_open.value()
        symbols = [ln.strip().upper() for ln in
                   self.symbols.toPlainText().splitlines() if ln.strip()]
        if symbols:
            s.symbols = symbols
        s.save()
        self.window.set_status(f"Settings saved to {settings_path()}")


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------
class AutobotWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = LauncherSettings.load()
        self.setWindowTitle(f"{APP_NAME} — dry run")
        self.setWindowIcon(app_icon(MARK))
        self.resize(1280, 860)
        self.setMinimumSize(1080, 720)

        self._live = False
        self._running = False
        self._last_error = ""

        self.worker = BotWorker(self.settings)
        self.worker.start_thread()

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
        self.dashboard = DashboardPage(self)
        self.backtest = BacktestPage(self)
        self.log = LogPage(self)
        self.settings_page = SettingsPage(self)
        for page in (self.dashboard, self.backtest, self.log, self.settings_page):
            self.stack.addWidget(page)
        content.addWidget(self.stack, 1)
        outer.addLayout(content, 1)
        outer.addWidget(self._build_statusbar())

        w = self.worker
        w.connected.connect(self._on_connected)
        w.connect_failed.connect(self._on_connect_failed)
        w.symbols_resolved.connect(self._on_symbols)
        w.snapshot.connect(self.dashboard.update_snapshot)
        w.event.connect(self.log.add)
        w.running_changed.connect(self._on_running_changed)
        w.backtest_done.connect(self.backtest.set_results)
        w.survey_done.connect(self.backtest.set_text)
        w.busy.connect(self._on_busy)
        w.failed.connect(self.show_error)

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._tick_clock)
        self.clock_timer.start(1000)

        self.set_status("Start MetaTrader 5, log in, then press Connect.")
        # Try immediately: the common case is that MT5 is already open, and
        # making the user press a button to discover that is friction for
        # nothing.
        self.do_connect()

    # ------------------------------------------------------------- chrome
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
            [("Dashboard", "pulse"), ("Backtest", "bars"),
             ("Activity", "waveform"), ("Settings", "sliders")]
        ):
            btn = QPushButton(f"  {label}")
            btn.setObjectName("NavButton")
            btn.setIcon(nav_icon(glyph, theme.TEXT_MUTED, theme.TEXT))
            btn.setIconSize(QSize(21, 21))
            btn.setCheckable(True)
            btn.setChecked(i == 0)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, idx=i: self.stack.setCurrentIndex(idx))
            self.nav_group.addButton(btn, i)
            lay.addWidget(btn)

        lay.addStretch(1)
        lay.addWidget(hairline())
        lay.addSpacing(14)
        self.mode_note = QLabel("Dry run\nNothing is sent to the broker.")
        self.mode_note.setObjectName("Hint")
        self.mode_note.setWordWrap(True)
        self.mode_note.setContentsMargins(12, 0, 12, 0)
        lay.addWidget(self.mode_note)
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
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Preferred)
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

    def _tick_clock(self) -> None:
        self.clock.setText(f"{datetime.now(timezone.utc):%H:%M:%S} UTC")

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setToolTip(text)

    def show_error(self, text: str) -> None:
        self._last_error = text
        self.details_btn.setVisible(True)
        self.set_status(text)

    def _show_last_error(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Autobot")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("The bot reported a problem.")
        box.setDetailedText(self._last_error)
        box.exec()

    def open_data_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.data_dir())))

    # ------------------------------------------------------------- actions
    def do_connect(self) -> None:
        self.worker.post(CONNECT)

    def on_live_toggled(self, live: bool) -> None:
        """Arm live mode, with a confirmation naming the account and the risk.

        Deliberately gated at the toggle rather than at Start: a checkbox that
        silently changes what Start means is the worst possible place to put
        this decision.
        """
        if not live:
            self._set_live(False)
            return
        equity = self.dashboard.tile_equity.value_label.text() \
            if hasattr(self.dashboard.tile_equity, "value_label") else "?"
        box = QMessageBox(self)
        box.setWindowTitle("Trade live?")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("Send real orders to this account?")
        box.setInformativeText(
            f"Equity {equity}. Risking {self.settings.risk_fraction:.2%} of it per "
            f"trade, stopping for the day at "
            f"-{self.settings.max_daily_loss_fraction:.0%}.\n\n"
            f"Backtest the strategy on this broker's own history first if you "
            f"have not.")
        box.setStandardButtons(QMessageBox.StandardButton.Cancel |
                               QMessageBox.StandardButton.Yes)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            self.dashboard.live_toggle.setChecked(False)
            return
        self._set_live(True)

    def _set_live(self, live: bool) -> None:
        self._live = live
        self.setWindowTitle(f"{APP_NAME} — {'LIVE' if live else 'dry run'}")
        self.dashboard.start_btn.setObjectName("Danger" if live else "Primary")
        # A stylesheet-driven object name change needs the style re-applied.
        self.dashboard.start_btn.style().unpolish(self.dashboard.start_btn)
        self.dashboard.start_btn.style().polish(self.dashboard.start_btn)
        self.mode_note.setText(
            "LIVE\nReal orders are being sent." if live
            else "Dry run\nNothing is sent to the broker.")
        self.mode_note.setStyleSheet(f"color: {theme.SELL};" if live else "")

    def do_start_stop(self) -> None:
        if self._running:
            self.worker.post(STOP)
        else:
            self.worker.post(START, live=self._live)

    # -------------------------------------------------------------- signals
    def _on_connected(self, diag: dict) -> None:
        self.dashboard.set_broker(diag)
        self.dashboard.start_btn.setEnabled(True)
        self.details_btn.setVisible(False)
        self.set_status(f"Connected to {diag['company']} account {diag['login']}.")

    def _on_connect_failed(self, error: str, help_lines: list[str]) -> None:
        self.dashboard.set_error(error, help_lines)
        self.dashboard.start_btn.setEnabled(False)
        self._last_error = "\n".join(help_lines)
        self.details_btn.setVisible(True)
        self.set_status("Not connected — see the Dashboard for what to try.")

    def _on_symbols(self, available: list[str], missing: list[str]) -> None:
        if missing:
            self.set_status(
                f"{len(available)} instrument(s) available. Not offered on this "
                f"account: {', '.join(missing)}")

    def _on_running_changed(self, running: bool, live: bool) -> None:
        self._running = running
        self.status_dot.set_state(running)
        self.dashboard.start_btn.setText("Stop" if running else "Start")
        self.dashboard.live_toggle.setEnabled(not running)
        self.dashboard.connect_btn.setEnabled(not running)
        if running:
            self.set_status("Running " + ("LIVE." if live else "as a dry run."))
        else:
            self.set_status("Stopped.")

    def _on_busy(self, message: str) -> None:
        if message:
            self.set_status(message)

    def closeEvent(self, event) -> None:      # noqa: N802 - Qt naming
        self.worker.shutdown()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon(MARK))
    app.setStyleSheet(theme.stylesheet())
    win = AutobotWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
