"""ORB Signals: the desktop window.

Alerts, not orders. There is no order code in this window, nothing it imports
can place one, and the data source it uses (``MT5Feed``) has no order methods at
all -- which is the point, and is why this app is safe to leave running.

What the window is for
----------------------
Two things a console cannot do. It holds the Telegram credentials so they are
entered once instead of exported as environment variables every session, and it
shows what each instrument is *doing* -- range marked, waiting, broke at 14:45,
retested -- so "no alerts yet" is distinguishable from "not working".

The signal feed is kept as a table rather than a scrolling log because the useful
question is "what did it send today", and a log answers that worst.
"""

from __future__ import annotations

import queue
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..markets import signals as sig
from ..markets.clock import session_for
from ..markets.execution import BUY
from ..markets.signal_settings import SignalSettings, settings_path
from . import theme
from .branding import app_icon
from .common import (card, cell, form_grid, form_note, form_row,
                     page_header, page_layout, scroll_host, table)
from .icons import icon as nav_icon
from .widgets import StatStrip, StatTile, StatusDot, hairline

APP_NAME = "ORB Signals"
APP_TAGLINE = "BREAK · RETEST"
MARK = "ORB"

KIND_COLOUR = {sig.BREAK: theme.GOLD, sig.RETEST: theme.GOLD_BRIGHT}


# --------------------------------------------------------------------------
# the worker
# --------------------------------------------------------------------------
class SignalWorker(QObject):
    """Owns the data connection and the only thread that touches it.

    The MetaTrader package wraps a process-global connection, so one thread
    talks to it and the window asks by posting commands. It also keeps the
    window responsive: a history pull blocks, and a blocked Qt main thread is a
    frozen window Windows offers to kill.
    """

    connected = Signal(dict)
    connect_failed = Signal(str)
    resolved = Signal(list, list)
    signal_found = Signal(object)
    note = Signal(str)
    running_changed = Signal(bool)
    states = Signal(list)
    busy = Signal(str)

    def __init__(self, settings: SignalSettings):
        super().__init__()
        self.settings = settings
        self._commands: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._source = None
        self._bot: Optional[sig.SignalBot] = None
        self._symbols: list[str] = []
        self._running = False
        self._seen_notes = 0

    def start_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="orb-signals",
                                            daemon=True)
            self._thread.start()

    def post(self, command: str, **kwargs) -> None:
        self._commands.put((command, kwargs))

    def shutdown(self) -> None:
        self.post("quit")
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    def _loop(self) -> None:
        while True:
            try:
                command, kwargs = self._commands.get(
                    timeout=max(1, self.settings.poll_seconds))
            except queue.Empty:
                if self._running:
                    self._tick()
                continue
            if command == "quit":
                self._close()
                return
            try:
                if command == "connect":
                    self._connect()
                elif command == "start":
                    self._start()
                elif command == "stop":
                    self._running = False
                    self.running_changed.emit(False)
                elif command == "tick":
                    self._tick()
            except Exception as exc:                    # pragma: no cover
                self.note.emit(f"{command} failed: {exc}")
                self.busy.emit("")
                traceback.print_exc()

    def _close(self) -> None:
        if self._source is not None:
            try:
                self._source.close()
            except Exception:
                pass
        self._source = None
        self._bot = None
        self._running = False

    def _connect(self) -> None:
        self.busy.emit("Connecting to MetaTrader 5...")
        self._close()
        try:
            from ..markets.mt5_bridge import MT5Feed

            # MT5Feed, deliberately: it can read history and quotes and has no
            # method that could place, modify or close anything.
            self._source = MT5Feed(
                login=self.settings.login, password="",
                server=self.settings.server,
                terminal_path=self.settings.terminal_path)
        except RuntimeError as exc:
            self.busy.emit("")
            self.connect_failed.emit(str(exc))
            return

        try:
            diag = self._source.diagnostics()
        except Exception as exc:
            self.busy.emit("")
            self.connect_failed.emit(str(exc))
            self._close()
            return
        self.connected.emit(diag)

        from ..markets.survey import resolve

        available, missing = resolve(self._source, self.settings.symbols)
        self._symbols = sorted(available.values())
        self.resolved.emit(self._symbols, missing)
        self.busy.emit("")

    def _start(self) -> None:
        if self._source is None or not self._symbols:
            self.note.emit("Connect to MetaTrader 5 first.")
            return
        config = self.settings.signal_config(self._symbols)
        try:
            self._bot = sig.SignalBot(self._source, config)
        except ValueError as exc:
            self.note.emit(str(exc))
            return
        self._bot.calibrate()
        self._drain_notes()
        self._running = True
        self.running_changed.emit(True)
        self._tick()

    def _drain_notes(self) -> None:
        if self._bot is None:
            return
        fresh = self._bot.notes[self._seen_notes:]
        self._seen_notes = len(self._bot.notes)
        for line in fresh:
            self.note.emit(line)

    def _tick(self) -> None:
        if self._bot is None:
            return
        for signal in self._bot.cycle():
            self.signal_found.emit(signal)
        self._drain_notes()
        self.states.emit(self._state_rows())

    def _state_rows(self) -> list[dict]:
        """One row per instrument: what it is doing, and the last reason given."""
        bot = self._bot
        now = datetime.now(timezone.utc)
        rows = []
        for symbol in self._symbols:
            row = {"symbol": symbol, "window": "--", "state": "waiting",
                   "range": "--", "detail": ""}
            try:
                spec = session_for(symbol)
            except KeyError:
                row["state"] = "not watched"
                row["detail"] = "no session mapped for this symbol"
                rows.append(row)
                continue
            day = spec.session_date(now)
            minutes = self.settings.range_minutes
            open_at = spec.open_utc(day)
            row["window"] = (f"{open_at:%H:%M}-"
                             f"{open_at + timedelta(minutes=minutes):%H:%M}")
            if not spec.is_open_weekday(day):
                row["state"] = "closed"
            elif now < open_at:
                row["state"] = "before open"
            elif now < open_at + timedelta(minutes=minutes):
                row["state"] = "range forming"
            if bot is not None:
                watch = bot.watches.get((symbol, day))
                if watch is not None:
                    row["state"] = watch.state
                    if watch.range_ is not None:
                        row["range"] = f"{watch.range_.width_points:.0f} pts"
                    if watch.reason:
                        row["detail"] = watch.reason
                last = bot._said.get(symbol)
                if last and not row["detail"]:
                    row["detail"] = last
            rows.append(row)
        return rows


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------
class FeedPage(QWidget):
    def __init__(self, window: "SignalsWindow"):
        super().__init__()
        self.window = window
        root = page_layout(self)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("Ghost")
        self.connect_btn.clicked.connect(lambda: window.worker.post("connect"))
        self.start_btn = QPushButton("Start watching")
        self.start_btn.setObjectName("Primary")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(window.toggle_running)

        root.addLayout(page_header(
            "Signals",
            "Marks the first 15 minutes after each market's open, then alerts on "
            "the break and again on the retest. It never places an order.",
            [self.connect_btn, self.start_btn]))

        self.tile_watching = StatTile("Watching")
        self.tile_breaks = StatTile("Breaks today")
        self.tile_retests = StatTile("Retests today")
        self.tile_alerts = StatTile("Telegram")
        root.addWidget(StatStrip([self.tile_watching, self.tile_breaks,
                                  self.tile_retests, self.tile_alerts]))

        broker_card, broker_lay = card("Data source")
        self.broker_text = QLabel("Not connected.")
        self.broker_text.setObjectName("Hint")
        self.broker_text.setWordWrap(True)
        self.broker_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        broker_lay.addWidget(self.broker_text)
        root.addWidget(broker_card)

        signals_card, signals_lay = card("Today's signals")
        self.signal_table = table(
            ["Time", "Stage", "Instrument", "Side", "Entry", "Stop", "Target",
             "Range"], stretch=7,
            widths={0: 70, 1: 80, 2: 110, 3: 60, 4: 95, 5: 95, 6: 95})
        self.signal_table.setMinimumHeight(180)
        signals_lay.addWidget(self.signal_table)
        root.addWidget(signals_card, 1)

        state_card, state_lay = card("What each instrument is doing")
        self.state_table = table(
            ["Instrument", "Range window", "Range", "State", "Why"], stretch=4,
            # 120 elided "Range window" to "!ange windov": QTableWidget centres
            # header text, so a header needs more width than its label alone.
            widths={0: 120, 1: 160, 2: 100, 3: 130})
        self.state_table.setMinimumHeight(170)
        state_lay.addWidget(self.state_table)
        root.addWidget(state_card, 1)

    def add_signal(self, signal) -> None:
        colour = KIND_COLOUR.get(signal.kind, theme.TEXT)
        side_colour = theme.BUY if signal.direction == BUY else theme.SELL
        row = 0
        self.signal_table.insertRow(0)
        values = [f"{signal.at:%H:%M}", signal.kind.upper(), signal.symbol,
                  signal.side, f"{signal.entry:.{signal.digits}f}",
                  f"{signal.stop:.{signal.digits}f}",
                  f"{signal.target:.{signal.digits}f}",
                  f"{signal.range_points:.0f} pts"]
        for c, value in enumerate(values):
            paint = colour if c == 1 else side_colour if c == 3 else None
            self.signal_table.setItem(row, c, cell(value, paint, mono=c >= 4))

    def set_states(self, rows: list[dict]) -> None:
        self.state_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            tint = (theme.GOLD if row["state"] in ("broken", "retested")
                    else theme.TEXT_FAINT if row["state"] in ("closed", "not watched",
                                                              "skipped", "failed")
                    else theme.TEXT)
            for c, value in enumerate([row["symbol"], row["window"], row["range"],
                                       row["state"], row["detail"]]):
                self.state_table.setItem(
                    r, c, cell(value, tint if c in (0, 3) else None,
                               mono=c in (1, 2)))


class SettingsPage(QWidget):
    """Everything editable, in a scroll area, laid out on a two-column grid.

    Both of those are load-bearing and were missing in the first version. A page
    taller than the window without a scroll area is not scrollable, it is
    *compressed* -- Qt shrinks each widget to fit, clipping the four-line
    Telegram instructions to one cut-off line and reducing every spin box and
    button to a sliver. And a per-row QHBoxLayout gives each row its own idea of
    where the control column starts, so on a wide screen the controls end up at
    the far right edge, a screen's width from their labels.
    """

    def __init__(self, window: "SignalsWindow"):
        super().__init__()
        self.window = window
        s = window.settings

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QWidget()
        head_lay = QVBoxLayout(head)
        head_lay.setContentsMargins(theme.PAGE_MARGIN_H, theme.PAGE_MARGIN_TOP,
                                    theme.PAGE_MARGIN_H, 0)
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        reveal = QPushButton("Open settings folder")
        reveal.setObjectName("Ghost")
        reveal.clicked.connect(window.open_data_folder)
        head_lay.addLayout(page_header(
            "Settings",
            f"Saved to {settings_path()}. Changes apply the next time you press "
            f"Start watching.", [reveal, save]))
        outer.addWidget(head)

        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(theme.PAGE_MARGIN_H, theme.GAP,
                                theme.PAGE_MARGIN_H, theme.PAGE_MARGIN_BOTTOM)
        root.setSpacing(theme.GAP_LG)

        # ---------------------------------------------------------- telegram
        tg_card, tg_lay = card("Telegram alerts")
        grid = form_grid()
        # One row per step rather than one label with newlines in it: a QLabel
        # sets its own line spacing from the font, which packed four numbered
        # steps into a block with the descenders almost touching. Separate rows
        # get the grid's row spacing between them and are legible as a list.
        r = 0
        for step in ("1. In Telegram, message @BotFather and send /newbot. It "
                     "replies with a token.",
                     "2. Paste the token below.",
                     "3. Open your new bot and send it any message — a bot cannot "
                     "start a conversation, so it has nothing to read until you do.",
                     "4. Press Find my chat ID."):
            r = form_note(grid, r, step)
        grid.setRowMinimumHeight(r, 10)
        r += 1

        self.tg_enabled = QCheckBox()
        self.tg_enabled.setChecked(s.telegram_enabled)
        r = form_row(grid, r, "Send alerts to Telegram", self.tg_enabled)

        self.tg_token = QLineEdit(s.telegram_token)
        self.tg_token.setPlaceholderText("123456789:AAE...")
        self.tg_token.setEchoMode(QLineEdit.EchoMode.Password)
        show = QCheckBox("Show")
        show.toggled.connect(lambda on: self.tg_token.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        token_row.addWidget(self.tg_token, 1)
        token_row.addWidget(show)
        r = form_row(grid, r, "Bot token", token_row)

        self.tg_chat = QLineEdit(s.telegram_chat_id)
        self.tg_chat.setPlaceholderText("e.g. 987654321")
        find = QPushButton("Find my chat ID")
        find.setObjectName("Ghost")
        find.clicked.connect(self.find_chat_id)
        chat_row = QHBoxLayout()
        chat_row.setSpacing(8)
        chat_row.addWidget(self.tg_chat, 1)
        chat_row.addWidget(find)
        r = form_row(grid, r, "Chat ID", chat_row)

        test = QPushButton("Send test message")
        test.setObjectName("Ghost")
        test.clicked.connect(self.send_test)
        test_row = QHBoxLayout()
        test_row.addWidget(test)
        test_row.addStretch(1)
        r = form_row(grid, r, "Check it works", test_row)

        self.alert_break = QCheckBox()
        self.alert_break.setChecked(s.alert_on_break)
        r = form_row(grid, r, "Alert on the break", self.alert_break)
        self.alert_retest = QCheckBox()
        self.alert_retest.setChecked(s.alert_on_retest)
        r = form_row(grid, r, "Alert on the retest", self.alert_retest)
        tg_lay.addLayout(grid)
        root.addWidget(tg_card)

        # ---------------------------------------------------------- strategy
        st_card, st_lay = card("Strategy")
        grid = form_grid()
        r = 0
        self.range_minutes = QSpinBox()
        self.range_minutes.setRange(1, 240)
        self.range_minutes.setValue(s.range_minutes)
        self.range_minutes.setSuffix(" min")
        r = form_row(grid, r, "Opening range length", self.range_minutes,
                     "The high and low of this many minutes after each market's open.")

        self.target_r = QDoubleSpinBox()
        self.target_r.setRange(0.25, 10.0)
        self.target_r.setSingleStep(0.25)
        self.target_r.setValue(s.target_r)
        self.target_r.setSuffix(" R")
        r = form_row(grid, r, "Target, in multiples of the stop", self.target_r)

        self.tolerance = QDoubleSpinBox()
        self.tolerance.setRange(0.0, 100.0)
        self.tolerance.setSingleStep(1.0)
        self.tolerance.setValue(s.retest_tolerance_fraction * 100)
        self.tolerance.setSuffix(" % of the range")
        r = form_row(grid, r, "How close a pullback counts as a retest",
                     self.tolerance,
                     "A pullback stopping a few points short of the level is still "
                     "a retest; demanding an exact touch misses most of them.")

        self.window_minutes = QSpinBox()
        self.window_minutes.setRange(10, 600)
        self.window_minutes.setValue(s.entry_window_minutes)
        self.window_minutes.setSuffix(" min")
        r = form_row(grid, r, "Stop looking for a break this long after the range",
                     self.window_minutes)

        self.filters = QCheckBox()
        self.filters.setChecked(s.apply_filters)
        r = form_row(grid, r, "Skip days whose range is too narrow to beat the spread",
                     self.filters)
        st_lay.addLayout(grid)
        root.addWidget(st_card)

        # -------------------------------------------------------- instruments
        sym_card, sym_lay = card("Instruments")
        sym_lay.addWidget(self._hint(
            "One per line, exactly as your platform names them. Broker suffixes "
            "are matched automatically, so XAUUSD finds XAUUSD247 or XAUUSD.r."))
        self.symbols = QTextEdit()
        self.symbols.setPlainText("\n".join(s.symbols))
        self.symbols.setMinimumHeight(170)
        self.symbols.setStyleSheet(f"font-family: {theme.MONO_STACK};")
        sym_lay.addWidget(self.symbols)
        root.addWidget(sym_card)

        # -------------------------------------------------------- connection
        conn_card, conn_lay = card("MetaTrader 5 (prices only)")
        conn_lay.addWidget(self._hint(
            "Used for price history. This app reads prices and cannot place, "
            "change or close a trade, so it needs no algo-trading permission."))
        grid = form_grid()
        self.terminal = QLineEdit(s.terminal_path or "")
        self.terminal.setPlaceholderText("Leave empty to use whichever MT5 is running")
        browse = QPushButton("Browse...")
        browse.setObjectName("Ghost")
        browse.clicked.connect(self._browse)
        term_row = QHBoxLayout()
        term_row.setSpacing(8)
        term_row.addWidget(self.terminal, 1)
        term_row.addWidget(browse)
        form_row(grid, 0, "Terminal", term_row)
        conn_lay.addLayout(grid)
        root.addWidget(conn_card)
        root.addStretch(1)

        outer.addWidget(scroll_host(body), 1)

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Hint")
        label.setWordWrap(True)
        label.setMinimumHeight(20 * (text.count("\n") + 1))
        return label

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Find terminal64.exe", "", "MetaTrader (terminal64.exe terminal.exe)")
        if path:
            self.terminal.setText(path)

    # ------------------------------------------------------------ telegram
    def find_chat_id(self) -> None:
        token = self.tg_token.text().strip()
        if not token:
            self.window.set_status("Paste the bot token first.")
            return
        from ..notifiers import discover_chat_ids

        chats, error = discover_chat_ids(token)
        if error:
            QMessageBox.information(self, "Find my chat ID", error)
            return
        if len(chats) == 1:
            self.tg_chat.setText(chats[0]["id"])
            self.window.set_status(f"Chat ID found: {chats[0]['name']}")
            return
        labels = [f"{c['name']}  ({c['id']})" for c in chats]
        choice, ok = QInputDialog.getItem(self, "Which chat?",
                                          "Send alerts to:", labels, 0, False)
        if ok and choice:
            self.tg_chat.setText(chats[labels.index(choice)]["id"])

    def send_test(self) -> None:
        token, chat = self.tg_token.text().strip(), self.tg_chat.text().strip()
        if not token or not chat:
            self.window.set_status("A token and a chat ID are both needed.")
            return
        from ..notifiers import TelegramNotifier

        ok, err = TelegramNotifier(token, chat).send_verbose(
            "*ORB Signals* connected. Break and retest alerts will arrive here.")
        if ok:
            self.window.set_status("Test message sent — check Telegram.")
        else:
            QMessageBox.warning(self, "Telegram", f"Could not send:\n\n{err}")

    # ---------------------------------------------------------------- save
    def save(self) -> None:
        s = self.window.settings
        s.telegram_enabled = self.tg_enabled.isChecked()
        s.telegram_token = self.tg_token.text().strip()
        s.telegram_chat_id = self.tg_chat.text().strip()
        s.alert_on_break = self.alert_break.isChecked()
        s.alert_on_retest = self.alert_retest.isChecked()
        s.range_minutes = self.range_minutes.value()
        s.target_r = self.target_r.value()
        s.retest_tolerance_fraction = self.tolerance.value() / 100.0
        s.entry_window_minutes = self.window_minutes.value()
        s.apply_filters = self.filters.isChecked()
        s.terminal_path = self.terminal.text().strip() or None
        symbols = [ln.strip().upper() for ln in
                   self.symbols.toPlainText().splitlines() if ln.strip()]
        if symbols:
            s.symbols = symbols

        problems = s.problems()
        if problems:
            QMessageBox.warning(self, "Check these settings",
                                "\n\n".join(f"• {p}" for p in problems))
        s.save()
        self.window.refresh_alert_tile()
        self.window.set_status(f"Saved to {settings_path()}")


class ActivityPage(QWidget):
    def __init__(self, window: "SignalsWindow"):
        super().__init__()
        root = page_layout(self)
        clear = QPushButton("Clear")
        clear.setObjectName("Ghost")
        clear.clicked.connect(lambda: self.view.clear())
        root.addLayout(page_header(
            "Activity",
            "Everything it looked at and every reason it gave for not alerting. "
            "\"No signals yet\" and \"not working\" look identical without this.",
            [clear]))
        body, lay = card()
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(3000)
        self.view.setStyleSheet(
            f"font-family: {theme.MONO_STACK}; font-size: 12px; "
            f"background: {theme.BG_ELEVATED}; border: none;")
        lay.addWidget(self.view)
        root.addWidget(body, 1)
        # An empty black box on a fresh start is indistinguishable from a broken
        # one, which is the confusion this page exists to prevent.
        self.add("Waiting for MetaTrader 5. Connection attempts, opening ranges, "
                 "breaks, retests and skip reasons all appear here.")

    def add(self, line: str) -> None:
        """One line, stamped with the local time it was observed.

        The stamp is not redundant with the times inside the messages: those are
        market times a bar carries, this is when the app saw it. A gap between
        them is how a stalled feed shows itself.
        """
        self.view.appendPlainText(f"{datetime.now():%H:%M:%S}  {line}")


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------
class SignalsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = SignalSettings.load()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon(MARK))
        self.resize(1240, 880)
        self.setMinimumSize(1040, 700)

        self._running = False
        self._breaks = 0
        self._retests = 0

        self.worker = SignalWorker(self.settings)
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
        self.feed = FeedPage(self)
        self.settings_page = SettingsPage(self)
        self.activity = ActivityPage(self)
        for page in (self.feed, self.settings_page, self.activity):
            self.stack.addWidget(page)
        content.addWidget(self.stack, 1)
        outer.addLayout(content, 1)
        outer.addWidget(self._build_statusbar())

        w = self.worker
        w.connected.connect(self._on_connected)
        w.connect_failed.connect(self._on_connect_failed)
        w.resolved.connect(self._on_resolved)
        w.signal_found.connect(self._on_signal)
        w.note.connect(self.activity.add)
        w.running_changed.connect(self._on_running)
        w.states.connect(self.feed.set_states)
        w.busy.connect(lambda text: text and self.set_status(text))

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._tick_clock)
        self.clock_timer.start(1000)

        self.refresh_alert_tile()
        self.set_status("Start MetaTrader 5, log in, then press Connect.")
        self.worker.post("connect")

    # ------------------------------------------------------------- chrome
    def _build_sidebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Sidebar")
        # 232 clipped "ORB Signals" to "ORB Signal" -- the brand mark is
        # letter-spaced, so it needs more room than its character count suggests.
        bar.setFixedWidth(252)
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
                [("Signals", "pulse"), ("Settings", "sliders"),
                 ("Activity", "waveform")]):
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
        note = QLabel("Alerts only.\nThis app cannot place a trade.")
        note.setObjectName("Hint")
        note.setWordWrap(True)
        note.setContentsMargins(12, 0, 12, 0)
        lay.addWidget(note)
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
        self.clock = QLabel()
        self.clock.setObjectName("StatusText")
        lay.addWidget(self.clock)
        return bar

    def _tick_clock(self) -> None:
        self.clock.setText(f"{datetime.now(timezone.utc):%H:%M:%S} UTC")

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setToolTip(text)

    def open_data_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.data_dir())))

    def refresh_alert_tile(self) -> None:
        ready = self.settings.telegram_ready
        self.feed.tile_alerts.set_value("on" if ready else "off",
                                        "" if ready else "set it up in Settings",
                                        theme.BUY if ready else None)

    # ------------------------------------------------------------ actions
    def toggle_running(self) -> None:
        self.worker.post("stop" if self._running else "start")

    def _on_connected(self, diag: dict) -> None:
        self.feed.broker_text.setText(
            f"{diag['company']}  ·  server {diag['server']}  ·  account "
            f"{diag['login']}\n{diag['terminal_path']}\n"
            f"Prices only — this app has no way to place an order.")
        self.feed.start_btn.setEnabled(True)
        self.set_status(f"Connected to {diag['company']}.")
        self.activity.add(f"Connected to {diag['company']}, server "
                          f"{diag['server']}, account {diag['login']}.")

    def _on_connect_failed(self, error: str) -> None:
        self.feed.broker_text.setText(
            f"Not connected.\n\n{error}\n\n"
            "Start MetaTrader 5 and log in, then press Connect. If your broker "
            "does not offer MetaTrader 5 at all, this app cannot read prices "
            "from it.")
        self.feed.start_btn.setEnabled(False)
        self.set_status("Not connected — see the Signals page.")
        self.activity.add(f"Not connected: {error}")

    def _on_resolved(self, available: list, missing: list) -> None:
        self.feed.tile_watching.set_value(str(len(available)))
        self.feed.set_states([])
        if missing:
            self.activity.add("not offered on this account: " + ", ".join(missing))

    def _on_signal(self, signal) -> None:
        self.feed.add_signal(signal)
        if signal.kind == sig.BREAK:
            self._breaks += 1
            self.feed.tile_breaks.set_value(str(self._breaks))
        else:
            self._retests += 1
            self.feed.tile_retests.set_value(str(self._retests))

        if not self.settings.wants(signal.kind):
            return
        if not self.settings.telegram_ready:
            self.activity.add(f"{signal.one_line()}  (Telegram is not set up, so "
                              f"this was not sent)")
            return
        from ..notifiers import TelegramNotifier

        ok, err = TelegramNotifier(self.settings.telegram_token,
                                   self.settings.telegram_chat_id).send_verbose(
            signal.format())
        self.activity.add(signal.one_line() + ("  → sent" if ok
                                               else f"  → Telegram failed: {err}"))

    def _on_running(self, running: bool) -> None:
        self._running = running
        self.status_dot.set_state(running)
        self.feed.start_btn.setText("Stop" if running else "Start watching")
        self.feed.connect_btn.setEnabled(not running)
        self.set_status("Watching." if running else "Stopped.")

    def closeEvent(self, event) -> None:      # noqa: N802 - Qt naming
        self.worker.shutdown()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon(MARK))
    app.setStyleSheet(theme.stylesheet())
    win = SignalsWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
