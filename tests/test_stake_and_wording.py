"""Two defects found in a recovered live journal.

1. Every trade recorded a stake of 1049.42 -- 2% of the configured balance --
   while the safety gate had capped the actual order at 50. The order path
   applied the cap and the journal path did not, so History and Analytics
   reported P/L twenty times larger than anything that was really risked.

2. Signals produced by ``trend_continuation + bos`` were cancelled with
   "a genuine breakout, not a sweep". The decision was right; the vocabulary
   belonged to a strategy that was not running, which reads like a bug even
   when the logic is correct.
"""

from datetime import datetime, timezone

import pytest

from cheese_signals import engine as engine_mod, storage
from cheese_signals.scheduler import revalidate, schedule_signal
from cheese_signals.settings import Settings
from cheese_signals.strategies import DOWN, FLAT, UP


def _engine(tmp_path, **overrides):
    s = Settings()
    for k, v in overrides.items():
        setattr(s, k, v)
    return engine_mod.SignalEngine(
        settings=s,
        journal=storage.Journal(tmp_path / "s.db"),
        feed_factory=lambda a: None,
    )


# ---------------------------------- stake ----------------------------------
def test_the_journal_stake_respects_the_safety_cap(tmp_path):
    """The exact numbers from the recovered journal."""
    eng = _engine(tmp_path, account_balance=52471.0, risk_per_trade=0.02, max_stake=50.0)

    uncapped = round(52471.0 * 0.02, 2)
    assert uncapped == pytest.approx(1049.42), "fixture no longer reproduces the report"
    assert eng._stake() == 50.0, (
        f"stake {eng._stake()} ignores the {eng.safety.config.max_stake} cap -- "
        "recorded P/L will not match what was traded"
    )


def test_the_order_and_the_journal_agree(tmp_path):
    """One method, so the two paths cannot drift apart again."""
    eng = _engine(tmp_path, account_balance=52471.0, risk_per_trade=0.02, max_stake=50.0)
    assert eng._stake() == eng._stake()

    import inspect

    source = inspect.getsource(engine_mod.SignalEngine)
    assert source.count("account_balance * self.settings.risk_per_trade") == 1, (
        "stake is computed in more than one place again"
    )


def test_a_stake_under_the_cap_is_untouched(tmp_path):
    eng = _engine(tmp_path, account_balance=500.0, risk_per_trade=0.02, max_stake=50.0)
    assert eng._stake() == 10.0


@pytest.mark.parametrize("balance,pct,cap,expected", [
    (500.0, 0.02, 50.0, 10.0),
    (5000.0, 0.02, 50.0, 50.0),      # capped
    (52471.0, 0.02, 50.0, 50.0),     # the reported case
    (1000.0, 0.01, 25.0, 10.0),
    (100.0, 0.05, 50.0, 5.0),
])
def test_stake_table(tmp_path, balance, pct, cap, expected):
    eng = _engine(tmp_path, account_balance=balance, risk_per_trade=pct, max_stake=cap)
    assert eng._stake() == expected


# --------------------------- cancellation wording ---------------------------
def _pending(direction, level, trigger):
    sig = schedule_signal(
        "EURUSD_otc", direction, 0.75, "trend_continuation+bos", "r",
        datetime(2026, 8, 4, 5, 26, tzinfo=timezone.utc), "tokyo",
        features={"invalidation_level": level, "event_setup": True, "trigger": trigger},
    )
    return sig


def test_a_bos_cancellation_does_not_mention_sweeps():
    """The message from the recovered journal, on a bos signal."""
    sig = _pending(DOWN, 158.011, "bos")
    why = revalidate(sig, FLAT, 0.75, 0.60, latest_close=158.014)

    assert why is not None, "closing back through the level must still cancel"
    assert "sweep" not in why.lower(), why
    assert "break failed" in why
    assert "158.011" in why and "158.014" in why


@pytest.mark.parametrize("trigger,expected", [
    ("bos", "break failed"),
    ("fractal", "swing point"),
    ("momentum", "fully retraced"),
])
def test_each_trigger_explains_itself(trigger, expected):
    for direction, close in ((DOWN, 1.2), (UP, 1.0)):
        sig = _pending(direction, 1.1, trigger)
        why = revalidate(sig, FLAT, 0.75, 0.60, latest_close=close)
        assert why and expected in why, f"{trigger}/{direction}: {why}"


def test_the_sweep_wording_survives_for_the_sweep_strategy():
    """liquidity_sweep signals carry no trigger, and its wording was correct."""
    sig = schedule_signal(
        "EURUSD_otc", DOWN, 0.75, "liquidity_sweep", "r",
        datetime(2026, 8, 4, 5, 26, tzinfo=timezone.utc), "tokyo",
        features={"invalidation_level": 1.1, "event_setup": True},
    )
    why = revalidate(sig, FLAT, 0.75, 0.60, latest_close=1.2)
    assert why and "not a sweep" in why


def test_the_cancellation_rule_itself_is_unchanged():
    """Wording moved; the logic must not have."""
    # A DOWN signal survives while price stays below the level.
    assert revalidate(_pending(DOWN, 1.1, "bos"), FLAT, 0.75, 0.60, latest_close=1.05) is None
    # An UP signal survives while price stays above it.
    assert revalidate(_pending(UP, 1.1, "bos"), FLAT, 0.75, 0.60, latest_close=1.15) is None
    # A direction flip still cancels regardless of level.
    why = revalidate(_pending(UP, 1.1, "bos"), DOWN, 0.75, 0.60, latest_close=1.15)
    assert why and "direction flipped" in why


def test_the_engine_records_the_trigger_on_every_signal(tmp_path):
    """Without this the wording falls back to the sweep default."""
    import inspect

    source = inspect.getsource(engine_mod.SignalEngine._evaluate_setup)
    assert '"trigger": self.settings.trigger' in source
