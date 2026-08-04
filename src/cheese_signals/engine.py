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

from . import confluence, diagnostics, execution, outcome as outcome_mod, profiles, sessions, storage
from . import indicators as ind
from . import strategies as strat
from . import trend as trend_mod
from . import setups as setups_mod
from .scheduler import ACTIVE, PendingSignal, SignalScheduler, revalidate, schedule_signal
from .settings import Settings
from .strategies import DOWN, FLAT, UP

Callback = Callable[..., None]

# How long to wait for the expiry candle to arrive before settling on
# whatever price is available.
SETTLEMENT_GRACE_SECONDS = 90.0

# How long an asset may go without gaining a candle before the shortfall is
# reported as a stall rather than as progress. Measured in seconds, not in
# scans: the loop revisits an asset far more often than candles arrive.
WARMUP_STALL_SECONDS = 8 * 60

# Journal history is merged in below this many live candles, and this many
# rows are read. Above the ceiling the live window already covers every
# indicator, so the query is skipped.
HISTORY_MERGE_CEILING = 600
HISTORY_MERGE_LIMIT = 1500


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
        on_trace: Optional[Callback] = None,
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
        self.on_trace = on_trace or (lambda *a, **k: None)
        self.traces = diagnostics.TraceLog()

        # Execution is opt-in and off by default; a signal engine must never
        # start placing trades as a side effect of running.
        self.trade_mode = getattr(settings, "trade_mode", execution.MODE_OFF)
        self.safety = execution.SafetyGate(execution.SafetyConfig(
            max_stake=getattr(settings, "max_stake", 50.0),
            max_concurrent=getattr(settings, "max_concurrent_trades", 3),
            max_trades_per_hour=settings.max_trades_per_hour,
            max_daily_loss=getattr(settings, "max_daily_loss", 100.0),
            min_balance=getattr(settings, "min_balance", 50.0),
            live_confirmed=getattr(settings, "live_confirmed", False),
        ))
        self.executor = None

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
        # asset -> (most candles seen, when that high-water mark was set)
        self._warmup_seen: dict[str, tuple[int, datetime]] = {}
        # asset -> (first seen warming up, candle count then) for the ETA
        self._warmup_started: dict[str, tuple[datetime, int]] = {}
        self._slow_cycle_reported = False

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
        self._trace(
            "engine", "STARTED",
            f"setup={self.settings.strategy} trigger={self.settings.trigger} "
            f"execution={self.trade_mode}"
            + (f" balance={self.executor.balance():.2f}" if self.executor else "")
            + f" | watching {len(self.settings.assets)} pairs",
        )
        for problem in self.settings.conflicts():
            self._trace("engine", "CONFIG WARNING", problem)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # never let one bad tick kill the engine
                self._report_error("engine", exc, traceback.format_exc())
            self._stop.wait(self.settings.poll_seconds)
        self.on_status("Engine stopped")

    def _trace(self, asset, outcome, summary, checks=(), candles=0, score=0.0, direction=0):
        t = diagnostics.make_trace(asset, outcome, summary, checks, candles, score, direction)
        self.traces.add(t)
        self.on_trace(t)

    def _report_error(self, asset: str, exc: BaseException, detail: str = "") -> None:
        """Record a failure everywhere it can be read.

        A one-line status bar cannot show a broker error that names sixty
        assets, so the full text goes to Diagnostics (and from there to the
        daily log file) while the status bar gets a summary. Losing the rest
        of the message is how a fixable configuration problem turns into "it
        just doesn't work".
        """
        text = str(exc) or exc.__class__.__name__
        lines = [ln for ln in text.splitlines() if ln.strip()]
        summary = lines[0] if lines else exc.__class__.__name__
        rest = lines[1:]
        if detail:
            rest = rest + detail.splitlines()

        self._trace(asset, "ERROR", summary, checks=rest)
        self.on_error(f"{asset}: {text}" if asset != "engine" else text)

    def _warmup_trace(self, asset: str, have: int, required: int, now: datetime) -> None:
        """Report a warm-up shortfall, with an ETA, and flag a genuine stall.

        Stalling is measured in *elapsed time*, not in scans. Counting scans
        was wrong by a wide margin: the loop revisits an asset every few
        seconds while a 1-minute candle arrives at most once a minute, so four
        consecutive scans without growth is the normal case, not a fault. That
        version cried BLOCKED 51 times on one asset in 26 minutes while the
        feed was filling perfectly well.
        """
        best, since = self._warmup_seen.get(asset, (0, now))
        if have > best:
            best, since = have, now
        self._warmup_seen[asset] = (best, since)

        stalled_for = (now - since).total_seconds()
        candle = max(self.settings.timeframe_seconds, 1)

        if stalled_for < WARMUP_STALL_SECONDS:
            rate = self._warmup_rate(asset, have, now)
            eta = ""
            if rate and rate > 0:
                minutes = (required - have) / rate
                eta = f" — about {minutes:.0f} min to go at {rate:.1f} candles/min"
            self._trace(
                asset, "WARMING UP",
                f"{have} candles, need {required} for {self.settings.strategy}{eta}",
                candles=have,
            )
            return

        shortfall = required - have
        self._trace(
            asset, "BLOCKED",
            f"stuck at {best} candles for {stalled_for / 60:.0f} min, {shortfall} short "
            f"of the {required} '{self.settings.strategy}' needs",
            checks=[
                f"No new candle in {stalled_for / candle:.0f} candle intervals — the feed "
                "has stopped delivering, so waiting is unlikely to fix this.",
                f"Lower the requirement: Settings -> Strategy -> Trend EMA period "
                f"(needs period + 20 bars; {best} candles allows about {max(best - 20, 5)}).",
                "Or pick a setup with a shorter warm-up: support_resistance needs "
                "lookback + 40, reversal needs about 60.",
            ],
            candles=have,
        )
        # Reset the clock so this is reported occasionally, not every scan.
        self._warmup_seen[asset] = (best, now)

    def _warmup_rate(self, asset: str, have: int, now: datetime) -> Optional[float]:
        """Candles per minute since this asset was first seen warming up."""
        first = self._warmup_started.setdefault(asset, (now, have))
        elapsed = (now - first[0]).total_seconds() / 60.0
        if elapsed < 2.0:
            return None      # too short a window to extrapolate from
        gained = have - first[1]
        return gained / elapsed if gained > 0 else None

    def _history_for(self, asset: str, live) -> Optional[pd.DataFrame]:
        """Live candles extended backwards with what the journal already holds.

        The broker's live stream only returns a couple of hours of history and
        refills it slowly, so an EMA 200 needs hours of uptime to warm up --
        and used to start from nothing again after every restart. The journal
        has been recording candles all along; reading them back makes the
        warm-up cumulative instead of per-session.

        The live rows win on overlap: they are the authoritative current view,
        and a stored bar could in principle be stale.
        """
        if live is None or len(live) == 0:
            return live
        if len(live) >= HISTORY_MERGE_CEILING:
            return live      # already plenty; skip the query entirely

        stored = self.journal.load_candles(asset, limit=HISTORY_MERGE_LIMIT)
        if stored is None or len(stored) == 0:
            return live

        merged = pd.concat([stored, live])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        return merged

    def _feed_for(self, asset: str):
        if asset not in self._feeds:
            self._feeds[asset] = self.feed_factory(asset)
        return self._feeds[asset]

    def _tick(self) -> None:
        started = time_mod.monotonic()
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
                self._report_error(asset, exc)

        self.scheduler.prune()
        self._check_cycle_time(time_mod.monotonic() - started)

    def _check_cycle_time(self, elapsed: float) -> None:
        """Warn when a full scan takes longer than a candle.

        Assets are scanned one after another. If a pass over the watchlist
        outlasts the timeframe, every pair is being looked at less often than
        once per candle -- so most closes are never evaluated, and a
        1-minute strategy silently becomes something else. Adding pairs feels
        free, and this is where it stops being free.
        """
        budget = self.settings.timeframe_seconds
        if elapsed <= budget or self._slow_cycle_reported:
            return

        self._slow_cycle_reported = True
        count = max(len(self.settings.assets), 1)
        self._trace(
            "engine", "CONFIG WARNING",
            f"a full scan of {count} pairs took {elapsed:.0f}s, longer than the "
            f"{budget}s candle — each pair is only checked every {elapsed / 60:.1f} min",
            checks=[
                "Most candle closes are going unevaluated, so signals will be missed.",
                f"At the measured {elapsed / count:.1f}s per pair, about "
                f"{max(int(budget / (elapsed / count)), 1)} pairs fit inside one candle.",
                "Trim the watchlist in Settings -> Pairs & Data to the pairs you "
                "actually trade.",
            ],
        )

    # ------------------------------ scanning ------------------------------
    def _scan_asset(self, asset: str, now: datetime) -> None:
        if not sessions.market_is_open(asset, now):
            self._trace(asset, "SKIPPED", "market closed for this instrument")
            return

        feed = self._feed_for(asset)
        live = feed.get_candles(500)

        # Store first, gate second. The other way round -- which is what this
        # did -- meant nothing was ever written while warming up, so every
        # restart began again from whatever short window the broker happened
        # to return, and the warm-up could never finish.
        self.journal.record_candles(asset, live)
        df = self._history_for(asset, live)

        # Each strategy needs enough history to warm its slowest indicator.
        # Reporting the shortfall matters: a feed that never returns enough
        # candles suppresses every signal forever, and used to do so silently.
        required = (
            self.settings.setup_config().min_bars()
            if self.settings.strategy in setups_mod.SETUPS else 120
        )
        have = 0 if df is None else len(df)
        if have < required:
            self._warmup_trace(asset, have, required, now)
            return
        self._warmup_seen.pop(asset, None)

        latest_ts = df.index[-1]
        if self._last_candle_ts.get(asset) == latest_ts:
            return  # no new closed candle yet; nothing has changed to report
        self._last_candle_ts[asset] = latest_ts

        profile = profiles.profile_for(asset)
        result, strategy_name, features = self._evaluate(df, asset, profile)
        checks = getattr(result, "checks", [])

        # Re-validate any pending signal for this asset against the new candle.
        for pending in list(self.scheduler.awaiting_entry(now)):
            if pending.asset != asset:
                continue
            reason = revalidate(
                pending,
                result.direction,
                result.score,
                min_score=self.settings.min_score,
                latest_close=float(df["close"].iloc[-1]),
            )
            if reason:
                self.scheduler.cancel(pending, reason)
                if pending.db_id is not None:
                    self.journal.set_signal_status(pending.db_id, "cancelled", reason)
                if self.notifier:
                    self.notifier.send_cancelled(pending, reason)
                self.on_cancel(pending, reason)

        if result.direction == FLAT:
            self._trace(asset, "NO SETUP", result.describe(), checks, len(df))
            return
        if result.score < self.settings.min_score:
            self._trace(
                asset, "SUPPRESSED",
                f"score {result.score:.2f} below minimum {self.settings.min_score:.2f}",
                checks, len(df), result.score, result.direction,
            )
            return

        if (
            self.settings.require_liquidity_sweep
            and self.settings.strategy != "trend_continuation"
            and strategy_name != "liquidity_sweep"
        ):
            return

        if self.scheduler.has_pending_for(asset):
            self._trace(asset, "SUPPRESSED", "a signal for this pair is already live",
                        checks, len(df), result.score, result.direction)
            return

        last = self._last_signal_ts.get(asset)
        if last and (now - last).total_seconds() < self.settings.cooldown_minutes * 60:
            wait = self.settings.cooldown_minutes * 60 - (now - last).total_seconds()
            self._trace(asset, "SUPPRESSED",
                        f"cooldown active, {wait:.0f}s remaining",
                        checks, len(df), result.score, result.direction)
            return

        session_label = sessions.session_label(now)
        if self.settings.restrict_to_sessions and self.settings.allowed_sessions:
            if session_label not in self.settings.allowed_sessions:
                self._trace(asset, "SUPPRESSED",
                            f"session '{session_label}' not in the allowed list",
                            checks, len(df), result.score, result.direction)
                return

        expiry_minutes = self.settings.expiry_minutes
        if self.settings.adaptive_expiry:
            advice = trend_mod.recommend_expiry(
                df, result.direction,
                min_minutes=self.settings.expiry_min_minutes,
                max_minutes=self.settings.expiry_max_minutes,
            )
            expiry_minutes = advice.minutes
            features["expiry_reason"] = advice.reason
            features["expiry_minutes"] = expiry_minutes

        signal = schedule_signal(
            asset=asset,
            direction=result.direction,
            score=result.score,
            strategy=strategy_name,
            reason=result.describe(),
            detected_at=now,
            session=session_label,
            lead_minutes=self.settings.lead_minutes,
            expiry_minutes=expiry_minutes,
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

        self._trace(
            asset, "FIRED",
            f"{'BUY' if signal.direction == UP else 'SELL'} entry {signal.entry_at:%H:%M:%S} "
            f"expiry {expiry_minutes}min score {signal.score:.2f}",
            checks, len(df), signal.score, signal.direction,
        )

        if self.notifier:
            self.notifier.send_signal(signal)
        self.on_signal(signal)

    def _evaluate(self, df: pd.DataFrame, asset: str, profile) -> tuple:
        """Run the strategies and return (result, winning_strategy_name, features)."""
        if self.settings.strategy in setups_mod.SETUPS:
            return self._evaluate_setup(df, profile)

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
            meta = sweep.meta
        else:
            chosen, name, score = conf, conf_tag, conf_score
            direction = conf.direction
            reason = conf.describe()
            # The confluence result carries the votes; take the level from
            # whichever vote actually set the direction.
            meta = next(
                (v.meta for v in conf.votes if v.is_actionable and v.direction == direction and v.meta),
                {},
            )

        bias = conf.bias
        features = {
            "adx": round(adx_val, 2) if adx_val == adx_val else None,
            "atr": round(atr_val, 6) if atr_val == atr_val else None,
            "displacement_atr": round(displacement, 3),
            "bias_aligned": None if bias == FLAT else (direction == bias),
            "profile": profile.name,
            "raw_score": round(chosen.score, 3),
            "weighted_score": round(score, 3),
            # Used by revalidate() to re-check the premise without re-detecting
            # a one-shot pattern that has already happened.
            "invalidation_level": meta.get("level"),
            "event_setup": bool(meta.get("event")),
        }

        class _R:
            pass

        r = _R()
        r.direction = direction
        r.score = min(score, 1.0)
        r.describe = lambda: reason
        return r, name, features

    def _evaluate_setup(self, df: pd.DataFrame, profile) -> tuple:
        """Run the configured setup + trigger, scored on real prices."""
        sig, checks = setups_mod.evaluate(
            df, self.settings.setup_config(), self.settings.trigger_config()
        )

        high, low, close = df["high"], df["low"], df["close"]
        adx_val = float(ind.adx(high, low, close).iloc[-1])
        atr_val = float(ind.atr(high, low, close).iloc[-1])
        o0, c0 = float(df["open"].iloc[-1]), float(close.iloc[-1])
        displacement = abs(c0 - o0) / atr_val if atr_val > 0 else 0.0

        features = {
            "adx": round(adx_val, 2) if adx_val == adx_val else None,
            "atr": round(atr_val, 6) if atr_val == atr_val else None,
            "displacement_atr": round(displacement, 3),
            "bias_aligned": None,
            "profile": profile.name,
            "raw_score": round(sig.score, 3),
            "weighted_score": round(sig.score, 3),
            "invalidation_level": sig.meta.get("level"),
            "event_setup": bool(sig.meta.get("event")),
            # Lets revalidate() explain a cancellation in the vocabulary of the
            # trigger that actually fired.
            "trigger": self.settings.trigger,
            "setup": self.settings.strategy,
        }

        class _R:
            pass

        r = _R()
        r.direction = sig.direction
        r.score = sig.score
        r.describe = lambda: sig.reason
        r.checks = [str(c) for c in checks]
        return r, f"{self.settings.strategy}+{self.settings.trigger}", features

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
    def _price_at(self, asset: str, ts: datetime) -> Optional[float]:
        """Close of the last candle that had closed at or before ``ts``.

        Entry and expiry must be priced at their own moments. Using "latest
        close" for both would compare a price against itself whenever the feed
        buffer had not ticked over yet, settling a live trade as a flat loss.
        """
        try:
            df = self._feed_for(asset).get_candles(60)
            if df is None or df.empty:
                return None
            stamp = pd.Timestamp(ts)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            if df.index.tz is None:
                df = df.tz_localize("UTC")
            at_or_before = df.index[df.index <= stamp]
            if len(at_or_before):
                return float(df.loc[at_or_before[-1], "close"])
            return float(df["close"].iloc[-1])
        except Exception:
            return None

    def _current_price(self, asset: str) -> Optional[float]:
        return self._price_at(asset, datetime.now(timezone.utc))

    def _enter(self, sig: PendingSignal, now: datetime) -> None:
        price = self._price_at(sig.asset, sig.entry_at)
        if price is None:
            self.scheduler.cancel(sig, "no price available at entry time")
            if sig.db_id is not None:
                self.journal.set_signal_status(sig.db_id, "cancelled", "no price at entry")
            return
        self.scheduler.mark_active(sig, price)
        if sig.db_id is not None:
            self.journal.set_signal_status(sig.db_id, ACTIVE)
        self.on_status(f"Entered {sig.asset} {sig.side} @ {price:.5f}")
        self._maybe_place_order(sig)

    def _stake(self) -> float:
        """The stake a trade actually uses, after the safety cap.

        One method, used by both the order path and the journal. They used to
        compute this separately and only the order path applied the cap, so a
        journal could record 1049.42 risked on a trade the safety gate had
        limited to 50 -- making every P/L figure in History and Analytics
        wrong by the ratio between them.
        """
        stake = round(self.settings.account_balance * self.settings.risk_per_trade, 2)
        return min(stake, self.safety.config.max_stake)

    def _maybe_place_order(self, sig: PendingSignal) -> None:
        """Place a trade for a signal, if execution is enabled and safe."""
        if self.executor is None or self.trade_mode == execution.MODE_OFF:
            return

        stake = self._stake()
        balance = self.executor.balance()

        ok, why = self.safety.check(self.trade_mode, stake, balance)
        if not ok:
            self._trace(sig.asset, "TRADE BLOCKED", f"{why} (stake {stake:.2f})")
            return

        expiry_seconds = int(
            (sig.expiry_at - sig.entry_at).total_seconds()
        ) or self.settings.expiry_minutes * 60
        try:
            order = self.executor.place(sig.asset, sig.direction, stake, expiry_seconds)
        except Exception as exc:
            self._trace(sig.asset, "TRADE FAILED", f"broker rejected the order: {exc}")
            self.on_error(f"{sig.asset}: order failed: {exc}")
            return

        self.safety.register_open()
        sig.order = order
        self._trace(
            sig.asset, "TRADE PLACED",
            f"{self.trade_mode.upper()} {sig.side} {stake:.2f} for {expiry_seconds}s"
            + (f" (broker id {order.broker_id})" if order.broker_id else ""),
        )
        self.on_status(f"{self.trade_mode.upper()} order placed: {sig.asset} {sig.side} {stake:.2f}")

    def _has_candle_at_or_after(self, asset: str, ts: datetime) -> bool:
        """Whether the candle covering ``ts`` has actually closed and arrived."""
        try:
            df = self._feed_for(asset).get_candles(5)
            if df is None or df.empty:
                return False
            stamp = pd.Timestamp(ts)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            last = df.index[-1]
            if last.tzinfo is None:
                last = last.tz_localize("UTC")
            return last >= stamp
        except Exception:
            return False

    def _settle(self, sig: PendingSignal, now: datetime) -> None:
        # The expiry candle closes *at* expiry_at and takes a moment to reach
        # the feed. Settling before it arrives prices the exit from the entry
        # candle, so exit == entry and the trade is recorded as a guaranteed
        # loss that never happened. Wait for the candle, within a grace period.
        if not self._has_candle_at_or_after(sig.asset, sig.expiry_at):
            waited = (now - sig.expiry_at).total_seconds()
            if waited < SETTLEMENT_GRACE_SECONDS:
                if waited < 2:   # report once, not on every poll
                    self._trace(sig.asset, "AWAITING CANDLE",
                                f"expiry reached; waiting for the {sig.expiry_at:%H:%M} candle")
                return  # try again on the next tick
            self.on_status(
                f"{sig.asset}: expiry candle never arrived after "
                f"{SETTLEMENT_GRACE_SECONDS:.0f}s; settling on last known price"
            )

        exit_price = self._price_at(sig.asset, sig.expiry_at)
        if exit_price is None or sig.entry_price is None:
            self.scheduler.mark_settled(sig)
            return

        stake = self._stake()
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
            ok, err = self.notifier.send_result(result)
            if not ok:
                # A result that never arrives looks like a bot that stopped
                # working, so say so rather than failing quietly.
                self._trace(sig.asset, "TELEGRAM FAILED",
                            f"could not deliver the {result.result_word} message: {err}")
                self.on_status(f"Telegram result failed: {err}")
            elif err:
                self._trace(sig.asset, "TELEGRAM", err)

        order = getattr(sig, "order", None)
        if order is not None and self.executor is not None:
            try:
                fill = self.executor.settle(order, result.exit_price, result.won, self.payout)
                self.safety.register_close(fill.profit)
                verdict = "WIN" if fill.won else "LOSS"
                note = " (broker-reported)" if fill.source == "broker" else ""
                self._trace(
                    sig.asset, "TRADE SETTLED",
                    f"{self.trade_mode.upper()} {verdict} {fill.profit:+.2f}{note}, "
                    f"balance {self.executor.balance():.2f}",
                )
                if self.safety.state.halted:
                    self._trace(sig.asset, "HALTED", self.safety.state.halt_reason)
                    self.on_status(f"Trading halted: {self.safety.state.halt_reason}")
            except Exception as exc:
                self.safety.register_close(-order.stake)
                self._trace(sig.asset, "TRADE SETTLE FAILED", str(exc))

        self._trace(
            sig.asset, "SETTLED",
            f"{result.result_word} {result.pnl:+.2f} "
            f"(entry {result.entry_price:.5f} -> exit {result.exit_price:.5f})",
        )
        self.on_result(result)
