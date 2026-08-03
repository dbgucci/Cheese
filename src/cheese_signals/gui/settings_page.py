"""Settings: every knob the engine reads, editable without a new build.

Two rules this page holds itself to:

1. **No decorative controls.** Every widget here maps to a field the engine
   actually consults. If a control cannot change behaviour it does not belong.
2. **Say when a setting is inert.** Options that only apply to one setup or
   trigger are disabled when that thing isn't selected, and combinations that
   cancel each other out are listed in a live warning panel rather than
   failing silently.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .. import setups as setups_mod
from .. import triggers as trig
from ..settings import DEFAULT_ASSETS
from . import theme


def _row(grid, r, label, widget, tip=""):
    lab = QLabel(label)
    if tip:
        lab.setToolTip(tip)
        widget.setToolTip(tip)
    grid.addWidget(lab, r, 0)
    grid.addWidget(widget, r, 1)
    grid.setColumnStretch(0, 1)
    return widget


def _group(title):
    box = QGroupBox(title)
    grid = QGridLayout(box)
    grid.setHorizontalSpacing(16)
    grid.setVerticalSpacing(9)
    return box, grid


def _hint(text):
    lab = QLabel(text)
    lab.setObjectName("Hint")
    lab.setWordWrap(True)
    return lab


def _group_hint(grid, row, text):
    """A description that belongs to the section above it, not the one below.

    Placed inside the group's own grid and spanning both columns, so the gap
    before the next section heading always reads as a section break.
    """
    lab = _hint(text)
    lab.setContentsMargins(0, 4, 0, 0)
    grid.addWidget(lab, row, 0, 1, 2)
    return lab


def _spin(lo, hi, val, step=1, suffix="", decimals=None):
    w = QDoubleSpinBox() if decimals is not None else QSpinBox()
    w.setRange(lo, hi)
    w.setValue(val)
    w.setSingleStep(step)
    if decimals is not None:
        w.setDecimals(decimals)
    if suffix:
        w.setSuffix(suffix)
    return w


class SettingsPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        s = window.settings

        root = QVBoxLayout(self)
        root.setContentsMargins(
            theme.PAGE_MARGIN_H, theme.PAGE_MARGIN_TOP,
            theme.PAGE_MARGIN_H, theme.PAGE_MARGIN_BOTTOM,
        )
        root.setSpacing(theme.GAP_LG)

        head = QVBoxLayout()
        head.setSpacing(5)
        t = QLabel("Settings")
        t.setObjectName("PageTitle")
        sub = QLabel("Everything the engine reads. Changes apply on the next engine start.")
        sub.setObjectName("PageSubtitle")
        head.addWidget(t)
        head.addWidget(sub)
        root.addLayout(head)

        self.warnings = QLabel()
        self.warnings.setObjectName("ConflictWarning")
        self.warnings.setWordWrap(True)
        self.warnings.setVisible(False)
        root.addWidget(self.warnings)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.tabs.addTab(self._tab_strategy(s), "Strategy")
        self.tabs.addTab(self._tab_timing(s), "Timing")
        self.tabs.addTab(self._tab_trading(s), "Trading")
        self.tabs.addTab(self._tab_pairs(s), "Pairs && Data")
        self.tabs.addTab(self._tab_alerts(s), "Alerts")

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 4, 0, 0)
        reset = QPushButton("Reset to Defaults")
        reset.setObjectName("Ghost")
        reset.clicked.connect(self.reset_defaults)
        footer.addWidget(reset)
        footer.addStretch(1)
        save = QPushButton("Save Settings")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        footer.addWidget(save)
        root.addLayout(footer)

        self._wire_live_updates()
        self.refresh_enablement()

    # ------------------------------ scroll host ------------------------------
    @staticmethod
    def _scroll(inner: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        return area

    # -------------------------------- tabs ----------------------------------
    def _tab_strategy(self, s):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 6, 14, 16)
        lay.setSpacing(10)

        box, grid = _group("Setup — what to look for")
        self.strategy = QComboBox(); self.strategy.addItems(setups_mod.SETUPS)
        self.strategy.setCurrentText(s.strategy)
        _row(grid, 0, "Setup", self.strategy)
        self.setup_help = _group_hint(grid, 1, setups_mod.SETUP_HELP.get(s.strategy, ""))
        lay.addWidget(box)

        box, grid = _group("Trigger — when to act")
        self.trigger = QComboBox(); self.trigger.addItems(trig.TRIGGERS)
        self.trigger.setCurrentText(s.trigger)
        _row(grid, 0, "Trigger", self.trigger)
        self.trigger_help = _group_hint(grid, 1, trig.TRIGGER_HELP.get(s.trigger, ""))
        lay.addWidget(box)

        # --- per-setup parameters ---
        self.box_trend, grid = _group("Trend continuation parameters")
        self.ema_trend = _row(grid, 0, "Trend EMA period", _spin(20, 500, s.ema_trend),
                              "The slow trend filter. 200 matches the chart setup.")
        self.keltner_ema = _row(grid, 1, "Keltner EMA period", _spin(2, 100, s.keltner_ema))
        self.keltner_atr = _row(grid, 2, "Keltner ATR period", _spin(2, 100, s.keltner_atr))
        self.keltner_mult = _row(grid, 3, "Keltner multiplier",
                                 _spin(0.1, 5.0, s.keltner_mult, 0.1, "", 2))
        self.require_ha = QCheckBox("Require Keltner and trend EMA to agree")
        self.require_ha.setChecked(s.require_ha_alignment)
        self.require_ha.setToolTip("Off means the trend EMA alone sets direction — more signals, less agreement.")
        grid.addWidget(self.require_ha, 4, 0, 1, 2)
        lay.addWidget(self.box_trend)

        self.box_sr, grid = _group("Support / resistance parameters")
        self.sr_lookback = _row(grid, 0, "Level lookback", _spin(5, 300, s.sr_lookback, 1, " candles"))
        self.sr_touch = _row(grid, 1, "Touch tolerance", _spin(0.0, 3.0, s.sr_touch_atr, 0.05, " ATR", 2),
                             "How close to the level counts as reaching it.")
        self.sr_reject = _row(grid, 2, "Rejection wick", _spin(0.1, 0.95, s.sr_reject_pct, 0.05, "", 2),
                              "Wick beyond the level as a fraction of the bar's range.")
        lay.addWidget(self.box_sr)

        self.box_rev, grid = _group("Reversal parameters")
        self.rsi_period = _row(grid, 0, "RSI period", _spin(2, 50, s.rsi_period))
        self.rsi_ob = _row(grid, 1, "Overbought above", _spin(50, 99, s.rsi_overbought, 1, "", 1))
        self.rsi_os = _row(grid, 2, "Oversold below", _spin(1, 50, s.rsi_oversold, 1, "", 1))
        self.bb_period = _row(grid, 3, "Bollinger period", _spin(2, 100, s.bb_period))
        self.bb_std = _row(grid, 4, "Bollinger std dev", _spin(0.5, 4.0, s.bb_std, 0.1, "", 1))
        lay.addWidget(self.box_rev)

        # --- per-trigger parameters ---
        self.box_frac, grid = _group("Fractal trigger parameters")
        self.fractal_age = _row(grid, 0, "Trigger window", _spin(0, 60, s.fractal_max_age, 1, " candles"),
                                "How recently the fractal must have been confirmed. "
                                "A fractal is only knowable 3 candles after it forms.")
        lay.addWidget(self.box_frac)

        self.box_bos, grid = _group("Break-of-structure parameters")
        self.bos_lookback = _row(grid, 0, "Swing lookback", _spin(3, 200, s.bos_lookback, 1, " candles"),
                                 "How far back a swing can be and still count as structure.")
        self.bos_buffer = _row(grid, 1, "Break buffer", _spin(0.0, 2.0, s.bos_buffer_atr, 0.05, " ATR", 2),
                               "Require the close to clear the level by this much. 0 = any break.")
        lay.addWidget(self.box_bos)

        self.box_mom, grid = _group("Momentum trigger parameters")
        self.mom_close = _row(grid, 0, "Close within", _spin(0.5, 0.99, s.momentum_close_pct, 0.05, "", 2),
                              "Close must finish in this top/bottom fraction of the bar's range.")
        self.mom_range = _row(grid, 1, "Minimum bar range", _spin(0.1, 5.0, s.momentum_range_atr, 0.1, " ATR", 2))
        lay.addWidget(self.box_mom)

        box, grid = _group("Filters (apply to every setup)")
        self.adx_min = _row(grid, 0, "ADX at least", _spin(0, 100, s.adx_min, 1, "", 1),
                            "0 disables the floor.")
        self.adx_max = _row(grid, 1, "ADX at most", _spin(0, 100, s.adx_max, 1, "", 1),
                            "100 disables the ceiling.")
        self.min_score = _row(grid, 2, "Minimum confidence", _spin(0.0, 1.0, s.min_score, 0.05, "", 2))
        self.cooldown = _row(grid, 3, "Cooldown per pair", _spin(0, 120, s.cooldown_minutes, 1, " min"))
        lay.addWidget(box)
        lay.addStretch(1)
        return self._scroll(page)

    def _tab_timing(self, s):
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 6, 14, 16); lay.setSpacing(10)

        box, grid = _group("Entry timing")
        self.lead = _row(grid, 0, "Advance warning", _spin(0, 10, s.lead_minutes, 1, " min"),
                         "How far ahead of entry a signal is announced. 0 = enter on the next candle.")
        self.timeframe = _row(grid, 1, "Candle size", _spin(5, 3600, s.timeframe_seconds, 5, " sec"))
        lay.addWidget(box)
        lay.addWidget(_hint(
            "A longer advance warning gives you more time to place a trade manually, but the "
            "market keeps moving in between. On a 1-minute expiry a 2-minute lead usually means "
            "the move is over before entry — the Analytics tab's 'By advance-warning time' table "
            "measures this on your own results."
        ))

        box, grid = _group("Expiry")
        self.adaptive = QCheckBox("Choose expiry automatically from recent move length")
        self.adaptive.setChecked(s.adaptive_expiry)
        grid.addWidget(self.adaptive, 0, 0, 1, 2)
        self.expiry_fixed = _row(grid, 1, "Fixed expiry", _spin(1, 60, s.expiry_minutes, 1, " min"),
                                 "Used when adaptive expiry is off.")
        self.expiry_min = _row(grid, 2, "Adaptive minimum", _spin(1, 60, s.expiry_min_minutes, 1, " min"))
        self.expiry_max = _row(grid, 3, "Adaptive maximum", _spin(1, 60, s.expiry_max_minutes, 1, " min"))
        lay.addWidget(box)

        box, grid = _group("Scanning")
        self.poll = _row(grid, 0, "Check for new candles every", _spin(1, 120, s.poll_seconds, 1, " sec"))
        self.bias_multiple = _row(grid, 1, "Higher-timeframe multiple", _spin(1, 60, s.bias_multiple))
        self.use_bias = QCheckBox("Use higher-timeframe bias as a filter")
        self.use_bias.setChecked(s.use_higher_timeframe_bias)
        grid.addWidget(self.use_bias, 2, 0, 1, 2)
        lay.addWidget(box)

        box, grid = _group("Sessions")
        self.restrict_sessions = QCheckBox("Only trade during selected sessions")
        self.restrict_sessions.setChecked(s.restrict_to_sessions)
        grid.addWidget(self.restrict_sessions, 0, 0, 1, 2)
        self.sessions_edit = QLineEdit(", ".join(s.allowed_sessions))
        self.sessions_edit.setPlaceholderText("london_ny_overlap, london, tokyo ...")
        _row(grid, 1, "Allowed sessions", self.sessions_edit)
        lay.addWidget(box)
        lay.addWidget(_hint(
            "OTC pairs are broker-generated and trade 24/7, so 'London liquidity' does not "
            "literally apply. Leave this off until the Analytics tab shows a real per-session "
            "difference in your own results."
        ))
        lay.addStretch(1)
        return self._scroll(page)

    def _tab_trading(self, s):
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 6, 14, 16); lay.setSpacing(10)

        box, grid = _group("Autotrading")
        self.trade_mode = QComboBox(); self.trade_mode.addItems(["off", "paper", "live"])
        self.trade_mode.setCurrentText(s.trade_mode)
        _row(grid, 0, "Execution mode", self.trade_mode)
        lay.addWidget(box)
        lay.addWidget(_hint(
            "off — signals only.\n"
            "paper — simulated trades against the real feed and a simulated balance. Use this "
            "to find out whether the strategy is worth trading, at no risk.\n"
            "live — places REAL orders with REAL money. Requires a typed confirmation."
        ))

        box, grid = _group("Position sizing")
        self.balance = _row(grid, 0, "Account balance", _spin(1, 1_000_000, s.account_balance, 10, "", 2))
        self.risk_pct = _row(grid, 1, "Risk per trade", _spin(0.1, 50.0, s.risk_per_trade * 100, 0.5, " %", 1))
        self.max_stake = _row(grid, 2, "Hard cap per trade", _spin(1, 100_000, s.max_stake, 5, "", 2),
                              "Applied after risk-per-trade. The smaller of the two wins.")
        lay.addWidget(box)

        box, grid = _group("Safety limits")
        self.max_concurrent = _row(grid, 0, "Max trades open at once", _spin(1, 50, s.max_concurrent_trades))
        self.max_per_hour = _row(grid, 1, "Max trades per hour", _spin(1, 200, s.max_trades_per_hour))
        self.max_daily_loss = _row(grid, 2, "Halt after losing", _spin(1, 1_000_000, s.max_daily_loss, 10, "", 2))
        self.min_balance = _row(grid, 3, "Never trade below balance", _spin(0, 1_000_000, s.min_balance, 10, "", 2))
        lay.addWidget(box)
        lay.addWidget(_hint(
            "These are hard limits checked before every order, and they fail closed: if a rule "
            "cannot be confirmed, the trade is not placed. Reaching the daily loss limit halts "
            "trading until you resume it."
        ))
        lay.addStretch(1)
        return self._scroll(page)

    def _tab_pairs(self, s):
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 6, 14, 16); lay.setSpacing(10)

        box, grid = _group("Pairs to watch")
        self.assets_edit = QTextEdit("\n".join(s.assets))
        self.assets_edit.setMaximumHeight(150)
        grid.addWidget(self.assets_edit, 0, 0, 1, 2)
        lay.addWidget(box)
        lay.addWidget(_hint("One per line. Pocket Option OTC symbols end in _otc, e.g. EURUSD_otc."))

        box, grid = _group("Data source")
        self.source = QComboBox(); self.source.addItems(["synthetic", "pocket_option"])
        self.source.setCurrentText(s.data_source)
        _row(grid, 0, "Feed", self.source)
        self.ssid = QLineEdit(s.pocket_option_ssid)
        self.ssid.setEchoMode(QLineEdit.EchoMode.Password)
        _row(grid, 1, "Pocket Option SSID", self.ssid)
        lay.addWidget(box)
        lay.addWidget(_hint(
            "'synthetic' generates practice data locally and needs no credentials. "
            "'pocket_option' uses your live feed and is required for live trading."
        ))
        lay.addStretch(1)
        return self._scroll(page)

    def _tab_alerts(self, s):
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 6, 14, 16); lay.setSpacing(10)

        box, grid = _group("Telegram")
        self.tg_enabled = QCheckBox("Send signal alerts and win/loss results")
        self.tg_enabled.setChecked(s.telegram_enabled)
        grid.addWidget(self.tg_enabled, 0, 0, 1, 2)
        self.tg_token = QLineEdit(s.telegram_bot_token)
        self.tg_token.setEchoMode(QLineEdit.EchoMode.Password)
        _row(grid, 1, "Bot token", self.tg_token)
        self.tg_chat = QLineEdit(s.telegram_chat_id)
        _row(grid, 2, "Chat ID", self.tg_chat)
        lay.addWidget(box)

        test = QPushButton("Send test message")
        test.setObjectName("Ghost")
        test.clicked.connect(self.test_telegram)
        lay.addWidget(test, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(_hint(
            "Diagnostics are never sent to Telegram — only signals and results. "
            "The Diagnostics tab stays on screen and in the log file."
        ))
        lay.addStretch(1)
        return self._scroll(page)

    # ---------------------------- live behaviour ----------------------------
    def _wire_live_updates(self):
        for w in (self.strategy, self.trigger, self.source, self.trade_mode):
            w.currentTextChanged.connect(lambda _=None: self.refresh_enablement())
        for w in (self.adaptive, self.restrict_sessions, self.tg_enabled, self.require_ha, self.use_bias):
            w.stateChanged.connect(lambda _=None: self.refresh_enablement())
        for w in (self.adx_min, self.adx_max, self.lead, self.expiry_fixed,
                  self.expiry_min, self.expiry_max, self.fractal_age, self.min_score,
                  self.balance, self.risk_pct, self.max_stake, self.min_balance):
            w.valueChanged.connect(lambda _=None: self.refresh_warnings())

    def refresh_enablement(self):
        """Grey out anything the current selection makes inert, and explain why."""
        setup = self.strategy.currentText()
        trigger = self.trigger.currentText()

        self.box_trend.setEnabled(setup == setups_mod.SETUP_TREND)
        self.box_sr.setEnabled(setup == setups_mod.SETUP_SR)
        self.box_rev.setEnabled(setup == setups_mod.SETUP_REVERSAL)
        for box, kind in ((self.box_frac, trig.TRIGGER_FRACTAL),
                          (self.box_bos, trig.TRIGGER_BOS),
                          (self.box_mom, trig.TRIGGER_MOMENTUM)):
            box.setEnabled(trigger == kind)

        self.setup_help.setText(setups_mod.SETUP_HELP.get(setup, ""))
        self.trigger_help.setText(trig.TRIGGER_HELP.get(trigger, ""))

        self.expiry_fixed.setEnabled(not self.adaptive.isChecked())
        self.expiry_min.setEnabled(self.adaptive.isChecked())
        self.expiry_max.setEnabled(self.adaptive.isChecked())
        self.sessions_edit.setEnabled(self.restrict_sessions.isChecked())
        self.ssid.setEnabled(self.source.currentText() == "pocket_option")
        for w in (self.tg_token, self.tg_chat):
            w.setEnabled(self.tg_enabled.isChecked())
        trading = self.trade_mode.currentText() != "off"
        for w in (self.max_stake, self.max_concurrent, self.max_daily_loss, self.min_balance):
            w.setEnabled(trading)
        self.refresh_warnings()

    def _pending_settings(self):
        """A copy of Settings reflecting the widgets, for conflict checking."""
        import copy
        s = copy.copy(self.window.settings)
        s.strategy = self.strategy.currentText()
        s.trigger = self.trigger.currentText()
        s.adx_min = self.adx_min.value(); s.adx_max = self.adx_max.value()
        s.min_score = self.min_score.value()
        s.fractal_max_age = self.fractal_age.value()
        s.momentum_close_pct = self.mom_close.value()
        s.lead_minutes = self.lead.value()
        s.adaptive_expiry = self.adaptive.isChecked()
        s.expiry_minutes = self.expiry_fixed.value()
        s.expiry_min_minutes = self.expiry_min.value()
        s.expiry_max_minutes = self.expiry_max.value()
        s.trade_mode = self.trade_mode.currentText()
        s.account_balance = self.balance.value()
        s.risk_per_trade = self.risk_pct.value() / 100.0
        s.max_stake = self.max_stake.value()
        s.min_balance = self.min_balance.value()
        s.restrict_to_sessions = self.restrict_sessions.isChecked()
        s.allowed_sessions = [x.strip() for x in self.sessions_edit.text().split(",") if x.strip()]
        s.assets = [a.strip() for a in self.assets_edit.toPlainText().splitlines() if a.strip()]
        return s

    def refresh_warnings(self):
        problems = self._pending_settings().conflicts()
        if problems:
            self.warnings.setText("⚠  " + "\n⚠  ".join(problems))
            self.warnings.setVisible(True)
        else:
            self.warnings.setVisible(False)

    # ------------------------------- actions --------------------------------
    def test_telegram(self):
        from ..notifiers import TelegramNotifier
        token, chat = self.tg_token.text().strip(), self.tg_chat.text().strip()
        if not token or not chat:
            QMessageBox.warning(self, "Telegram", "Enter both a bot token and a chat ID first.")
            return
        ok, msg = TelegramNotifier(token, chat).test()
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Telegram", msg)

    def reset_defaults(self):
        from ..settings import Settings
        if QMessageBox.question(
            self, "Reset settings",
            "Restore every setting to its default? Your trade history is not affected.",
        ) != QMessageBox.StandardButton.Yes:
            return
        defaults = Settings()
        defaults.save()
        self.window.settings = defaults
        QMessageBox.information(self, "Settings", "Defaults restored. Reopen Settings to see them.")

    def save(self):
        mode = self.trade_mode.currentText()
        confirmed = getattr(self.window.settings, "live_confirmed", False)
        if mode == "live" and not confirmed:
            typed, ok = QInputDialog.getText(
                self, "Confirm live trading",
                "This places REAL orders with REAL money on your Pocket Option account,\n"
                "automatically, without asking again.\n\n"
                "Type  TRADE LIVE  to confirm, or Cancel to stay in paper mode:",
            )
            if not ok or typed.strip() != "TRADE LIVE":
                self.trade_mode.setCurrentText("paper")
                mode = "paper"
                QMessageBox.information(self, "Autotrading", "Not confirmed — left in paper mode.")
            else:
                confirmed = True
        if mode != "live":
            confirmed = False

        assets = [a.strip() for a in self.assets_edit.toPlainText().splitlines() if a.strip()]
        self.window.settings.update(
            strategy=self.strategy.currentText(),
            trigger=self.trigger.currentText(),
            ema_trend=self.ema_trend.value(),
            keltner_ema=self.keltner_ema.value(),
            keltner_atr=self.keltner_atr.value(),
            keltner_mult=self.keltner_mult.value(),
            require_ha_alignment=self.require_ha.isChecked(),
            sr_lookback=self.sr_lookback.value(),
            sr_touch_atr=self.sr_touch.value(),
            sr_reject_pct=self.sr_reject.value(),
            rsi_period=self.rsi_period.value(),
            rsi_overbought=self.rsi_ob.value(),
            rsi_oversold=self.rsi_os.value(),
            bb_period=self.bb_period.value(),
            bb_std=self.bb_std.value(),
            fractal_max_age=self.fractal_age.value(),
            bos_lookback=self.bos_lookback.value(),
            bos_buffer_atr=self.bos_buffer.value(),
            momentum_close_pct=self.mom_close.value(),
            momentum_range_atr=self.mom_range.value(),
            adx_min=self.adx_min.value(),
            adx_max=self.adx_max.value(),
            min_score=self.min_score.value(),
            cooldown_minutes=self.cooldown.value(),
            lead_minutes=self.lead.value(),
            timeframe_seconds=self.timeframe.value(),
            adaptive_expiry=self.adaptive.isChecked(),
            expiry_minutes=self.expiry_fixed.value(),
            expiry_min_minutes=self.expiry_min.value(),
            expiry_max_minutes=max(self.expiry_max.value(), self.expiry_min.value()),
            poll_seconds=self.poll.value(),
            bias_multiple=self.bias_multiple.value(),
            use_higher_timeframe_bias=self.use_bias.isChecked(),
            restrict_to_sessions=self.restrict_sessions.isChecked(),
            allowed_sessions=[x.strip() for x in self.sessions_edit.text().split(",") if x.strip()],
            trade_mode=mode,
            live_confirmed=confirmed,
            account_balance=self.balance.value(),
            risk_per_trade=self.risk_pct.value() / 100.0,
            max_stake=self.max_stake.value(),
            max_concurrent_trades=self.max_concurrent.value(),
            max_trades_per_hour=self.max_per_hour.value(),
            max_daily_loss=self.max_daily_loss.value(),
            min_balance=self.min_balance.value(),
            assets=assets or list(DEFAULT_ASSETS),
            data_source=self.source.currentText(),
            pocket_option_ssid=self.ssid.text().strip(),
            telegram_enabled=self.tg_enabled.isChecked(),
            telegram_bot_token=self.tg_token.text().strip(),
            telegram_chat_id=self.tg_chat.text().strip(),
        )
        self.refresh_warnings()
        problems = self.window.settings.conflicts()
        extra = ("\n\nNote:\n• " + "\n• ".join(problems)) if problems else ""
        self.window.set_status("Settings saved. Restart the engine to apply.")
        QMessageBox.information(self, "Settings", "Saved." + extra)
