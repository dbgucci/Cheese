"""The live trading engine: detection -> advance alert -> entry -> expiry -> journal.

Runs on a background thread so the GUI stays responsive. Emits plain callbacks
(``on_signal``, ``on_cancel``, ``on_result``, ``on_status``) rather than Qt
signals, so the engine stays importable and testable without a GUI.
"""

from __future__ import annotations

import threading
import time as time_mod
import traceback
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from . import confluence, outcome as outcome_mod, profiles, sessions, storage
from . import indicators as ind
from . import strategies as strat
from .scheduler import ACTIVE, PendingSignal, SignalScheduler, revalidate, schedule_signal
from .settings import Settings
from .strategies import DOWN, FLAT, UP

Callback = Callable[..., None]


class SignalEngine:
    def __init__(
        self,
        settings: Settings,
        journal: storage.Journal,
        feed_factory: Callable[[str], object],
        notifier=None,
        payout: float = 0.85,
        on_signal: Optional[Callback] = None,
        on_cancel: Optional[Callback] = None,
        on_result: Optional[Callback] = None,
        on_status: Optional[Callback] = None,
        on_error: Optional[Callback] = None,
    ):
        self.settings = settings
        self.journal = journal
        self.feed_factory = feed_factory
        self.notifier = notifier
        self.payout = payout

        self.on_signal = on_signal or (lambda *a, **k: None)
        self.on_cancel = on_cancel or (lambda *a, **k: None)
        self.on_result = on_result or (lambda *a, **k: None)
        self.on_status = on_status or (lambda *a, **k: None)
        self.on_error = on_error or (lambda *a, **k: None)

        self.scheduler = SignalScheduler(
            lead_minutes=settings.lead_minutes,
            expiry_minutes=settings.expiry_minutes,
            timeframe_seconds=settings.timeframe_seconds,
        )
        self._feeds: dict[str, object] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_candle_ts: dict[str, pd.Timestamp] = {}
        self._last_signal_ts: dict[str, datetime] = {}

    # ------------------------------ lifecycle ------------------------------
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="signal-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # -------------------------------- loop --------------------------------
    def _run(self) -> None:
        self.on_status("Engine started")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # never let one bad tick kill the engine
                self.on_error(f"{exc}\n{traceback.format_exc()}")
            self._stop.wait(self.settings.poll_seconds)
        self.on_status("Engine stopped")

    def _feed_for(self, asset: str):
        if asset not in self._feeds:
            self._feeds[asset] = self.feed_factory(asset)
        return self._feeds[asset]

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)

        # 1. Settle anything that has reached expiry.
        for sig in self.scheduler.due_for_settlement(now):
            self._settle(sig, now)

        # 2. Enter anything whose entry minute has arrived.
        for sig in self.scheduler.due_for_entry(now):
            self._enter(sig, now)

        # 3. Scan each asset for new setups / re-validate pending ones.
        for asset in self.settings.assets:
            if self._stop.is_set():
                return
            try:
                self._scan_asset(asset, now)
            except Exception as exc:
                self.on_error(f"{asset}: {exc}")

        self.scheduler.prune()

    # ------------------------------ scanning ------------------------------
    def _scan_asset(self, asset: str, now: datetime) -> None:
        if not sessions.market_is_open(asset, now):
            return

        feed = self._feed_for(asset)
        df = feed.get_candles(400)
        if df is None or len(df) < 120:
            return

        latest_ts = df.index[-1]
        if self._last_candle_ts.get(asset) == latest_ts:
            return  # no new closed candle yet
        self._last_candle_ts[asset] = latest_ts

        self.journal.record_candles(asset, df.tail(50))

        profile = profiles.profile_for(asset)
        result, strategy_name, features = self._evaluate(df, asset, profile)

        # Re-validate any pending signal for this asset against the new candle.
        for pending in list(self.scheduler.awaiting_entry(now)):
            if pending.asset != asset:
                continue
            reason = revalidate(
                pending,
                result.direction,
                result.score,
                min_score=self.settings.min_score,
            )
            if reason:
                self.scheduler.cancel(pending, reason)
                if pending.db_id is not None:
                    self.journal.set_signal_status(pending.db_id, "cancelled", reason)
                if self.notifier:
                    self.notifier.send_cancelled(pending, reason)
                self.on_cancel(pending, reason)

        if result.direction == FLAT or result.score < self.settings.min_score:
            return

        if self.settings.require_liquidity_sweep and strategy_name != "liquidity_sweep":
            return

        if self.scheduler.has_pending_for(asset):
            return  # one live signal per asset at a time

        last = self._last_signal_ts.get(asset)
        if last and (now - last).total_seconds() < self.settings.cooldown_minutes * 60:
            return

        session_label = sessions.session_label(now)
        if self.settings.restrict_to_sessions and self.settings.allowed_sessions:
            if session_label not in self.settings.allowed_sessions:
                return

        signal = schedule_signal(
            asset=asset,
            direction=result.direction,
            score=result.score,
            strategy=strategy_name,
            reason=result.describe(),
            detected_at=now,
            session=session_label,
            lead_minutes=self.settings.lead_minutes,
            expiry_minutes=self.settings.expiry_minutes,
            timeframe_seconds=self.settings.timeframe_seconds,
            features=features,
        )
        signal.db_id = self.journal.record_signal(
            asset=asset,
            direction=signal.direction,
            score=signal.score,
            strategy=strategy_name,
            reason=signal.reason,
            detected_at=signal.detected_at,
            entry_at=signal.entry_at,
            expiry_at=signal.expiry_at,
            session=session_label,
            utc_hour=now.hour,
            features=features,
        )
        self.scheduler.add(signal)
        self._last_signal_ts[asset] = now

        if self.notifier:
            self.notifier.send_signal(signal)
        self.on_signal(signal)

    def _evaluate(self, df: pd.DataFrame, asset: str, profile) -> tuple:
        """Run the strategies and return (result, winning_strategy_name, features)."""
        sweep = strat.liquidity_sweep(df)
        conf = confluence.evaluate(
            df,
            self._bias_frame(df) if self.settings.use_higher_timeframe_bias else None,
            threshold=self.settings.min_score,
        )

        high, low, close = df["high"], df["low"], df["close"]
        adx_val = float(ind.adx(high, low, close).iloc[-1])
        atr_val = float(ind.atr(high, low, close).iloc[-1])
        o0, c0 = float(df["open"].iloc[-1]), float(close.iloc[-1])
        displacement = abs(c0 - o0) / atr_val if atr_val > 0 else 0.0

        weights = profile.weights
        sweep_score = sweep.score * weights.get("liquidity_sweep", 1.0)
        conf_tag = conf.votes[0].tags[0] if conf.votes and conf.votes[0].tags else "trend"
        conf_score = conf.score * weights.get(conf_tag, 1.0)

        if sweep.is_actionable and sweep_score >= conf_score:
            chosen, name, score = sweep, "liquidity_sweep", sweep_score
            direction = sweep.direction
            reason = sweep.reason
        else:
            chosen, name, score = conf, conf_tag, conf_score
            direction = conf.direction
            reason = conf.describe()

        bias = conf.bias
        features = {
            "adx": round(adx_val, 2) if adx_val == adx_val else None,
            "atr": round(atr_val, 6) if atr_val == atr_val else None,
            "displacement_atr": round(displacement, 3),
            "bias_aligned": None if bias == FLAT else (direction == bias),
            "profile": profile.name,
            "raw_score": round(chosen.score, 3),
            "weighted_score": round(score, 3),
        }

        class _R:
            pass

        r = _R()
        r.direction = direction
        r.score = min(score, 1.0)
        r.describe = lambda: reason
        return r, name, features

    def _bias_frame(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        mult = self.settings.bias_multiple
        if mult <= 1:
            return None
        rule = f"{mult * self.settings.timeframe_seconds}s"
        return (
            df.resample(rule)
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            .dropna()
        )

    # --------------------------- entry / settle ---------------------------
    def _current_price(self, asset: str) -> Optional[float]:
        try:
            df = self._feed_for(asset).get_candles(3)
            if df is None or df.empty:
                return None
            return float(df["close"].iloc[-1])
        except Exception:
            return None

    def _enter(self, sig: PendingSignal, now: datetime) -> None:
        price = self._current_price(sig.asset)
        if price is None:
            self.scheduler.cancel(sig, "no price available at entry time")
            if sig.db_id is not None:
                self.journal.set_signal_status(sig.db_id, "cancelled", "no price at entry")
            return
        self.scheduler.mark_active(sig, price)
        if sig.db_id is not None:
            self.journal.set_signal_status(sig.db_id, ACTIVE)
        self.on_status(f"Entered {sig.asset} {sig.side} @ {price:.5f}")

    def _settle(self, sig: PendingSignal, now: datetime) -> None:
        exit_price = self._current_price(sig.asset)
        if exit_price is None or sig.entry_price is None:
            self.scheduler.mark_settled(sig)
            return

        stake = round(self.settings.account_balance * self.settings.risk_per_trade, 2)
        result = outcome_mod.settle(
            sig, sig.entry_price, exit_price, stake=stake, payout=self.payout, settled_at=now
        )
        self.scheduler.mark_settled(sig)

        if sig.db_id is not None:
            self.journal.record_outcome(
                signal_id=sig.db_id,
                asset=sig.asset,
                direction=sig.direction,
                entry_price=result.entry_price,
                exit_price=result.exit_price,
                won=result.won,
                payout=self.payout,
                stake=stake,
                settled_at=now,
                reason=result.reason,
            )

        if self.notifier:
            self.notifier.send_result(result)
        self.on_result(result)
