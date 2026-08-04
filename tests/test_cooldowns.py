"""Per-pair cooldowns after a win and after a loss.

These are concentration controls: they stop one pair dominating the book.
They are deliberately *not* sold as an edge, because the measurement that
motivated them does not support one. On 190 live trades, the next trade on a
pair scored 50.5% after a win (n=99) and 58.8% after a loss (n=85) --
Fisher p=0.30, and the after-a-win effect reversed between halves (39.5%
then 58.9%). Hence a modest win default and a loss default of zero.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from cheese_signals import engine as engine_mod, storage
from cheese_signals.settings import Settings

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _engine(tmp_path, **overrides):
    s = Settings()
    s.assets = ["EURUSD_otc"]
    for k, v in overrides.items():
        setattr(s, k, v)
    return engine_mod.SignalEngine(
        settings=s, journal=storage.Journal(tmp_path / "c.db"),
        feed_factory=lambda a: None,
    )


def _held(eng, minutes_later):
    return eng._cooldown_remaining("EURUSD_otc", NOW + timedelta(minutes=minutes_later))


# ------------------------------- after a win -------------------------------
def test_a_win_rests_the_pair_for_the_configured_window(tmp_path):
    eng = _engine(tmp_path, cooldown_minutes=3, win_cooldown_minutes=5)
    eng._last_outcome["EURUSD_otc"] = (True, NOW)

    held = _held(eng, 2)
    assert held and "after a win" in held[1]
    assert held[0] == pytest.approx(3 * 60)

    assert _held(eng, 6) is None, "the pair should be free once the window passes"


def test_a_win_cooldown_of_zero_does_nothing(tmp_path):
    eng = _engine(tmp_path, cooldown_minutes=0, win_cooldown_minutes=0)
    eng._last_outcome["EURUSD_otc"] = (True, NOW)
    assert _held(eng, 0.1) is None


# ------------------------------ after a loss -------------------------------
def test_the_loss_cooldown_is_off_by_default(tmp_path):
    """The data says a pair does BETTER after a loss, so resting it costs."""
    eng = _engine(tmp_path, cooldown_minutes=0)
    assert eng.settings.loss_cooldown_minutes == 0
    eng._last_outcome["EURUSD_otc"] = (False, NOW)
    assert _held(eng, 0.1) is None


def test_a_loss_cooldown_applies_when_turned_on(tmp_path):
    eng = _engine(tmp_path, cooldown_minutes=3, loss_cooldown_minutes=10)
    eng._last_outcome["EURUSD_otc"] = (False, NOW)

    held = _held(eng, 4)
    assert held and "after a loss" in held[1]
    assert held[0] == pytest.approx(6 * 60)
    assert _held(eng, 11) is None


def test_win_and_loss_windows_are_independent(tmp_path):
    eng = _engine(tmp_path, cooldown_minutes=0, win_cooldown_minutes=20,
                  loss_cooldown_minutes=2)
    eng._last_outcome["EURUSD_otc"] = (False, NOW)
    assert _held(eng, 5) is None, "a loss must not use the win window"

    eng._last_outcome["EURUSD_otc"] = (True, NOW)
    assert _held(eng, 5) is not None, "a win must not use the loss window"


# ------------------------------ rule priority ------------------------------
def test_the_longest_applicable_cooldown_wins(tmp_path):
    eng = _engine(tmp_path, cooldown_minutes=30, win_cooldown_minutes=5)
    eng._last_signal_ts["EURUSD_otc"] = NOW
    eng._last_outcome["EURUSD_otc"] = (True, NOW)

    held = _held(eng, 10)
    assert held and held[1] == "cooldown active", held
    assert held[0] == pytest.approx(20 * 60)


def test_the_base_cooldown_still_works_with_no_outcome_yet(tmp_path):
    eng = _engine(tmp_path, cooldown_minutes=3)
    eng._last_signal_ts["EURUSD_otc"] = NOW
    held = _held(eng, 1)
    assert held and held[1] == "cooldown active"
    assert _held(eng, 4) is None


def test_cooldowns_are_per_pair(tmp_path):
    eng = _engine(tmp_path, cooldown_minutes=0, win_cooldown_minutes=30)
    eng.settings.assets = ["EURUSD_otc", "GBPUSD_otc"]
    eng._last_outcome["EURUSD_otc"] = (True, NOW)

    assert eng._cooldown_remaining("EURUSD_otc", NOW + timedelta(minutes=1)) is not None
    assert eng._cooldown_remaining("GBPUSD_otc", NOW + timedelta(minutes=1)) is None


# ------------------------------- plumbing ----------------------------------
def test_settling_a_trade_records_the_outcome_for_the_cooldown(tmp_path):
    """Without this the win/loss windows never start."""
    import inspect

    source = inspect.getsource(engine_mod.SignalEngine._settle)
    assert "_last_outcome[sig.asset]" in source


def test_a_block_says_which_rule_caused_it(tmp_path):
    """Diagnostics has to distinguish the three reasons a pair is quiet."""
    eng = _engine(tmp_path, cooldown_minutes=3, win_cooldown_minutes=20,
                  loss_cooldown_minutes=15)
    labels = set()
    eng._last_signal_ts["EURUSD_otc"] = NOW
    labels.add(_held(eng, 1)[1])
    eng._last_signal_ts.clear()
    eng._last_outcome["EURUSD_otc"] = (True, NOW)
    labels.add(_held(eng, 1)[1])
    eng._last_outcome["EURUSD_otc"] = (False, NOW)
    labels.add(_held(eng, 1)[1])
    assert len(labels) == 3, labels


# ------------------------------- conflicts ---------------------------------
def test_a_cooldown_shorter_than_the_base_is_flagged_as_inert():
    s = Settings(); s.cooldown_minutes = 10; s.win_cooldown_minutes = 5
    assert any("never applies" in c for c in s.conflicts())


def test_a_cooldown_that_starves_the_engine_is_flagged():
    s = Settings(); s.win_cooldown_minutes = 60
    assert any("signals an hour" in c for c in s.conflicts())


def test_the_recommended_defaults_raise_no_conflict():
    s = Settings()
    assert s.win_cooldown_minutes == 5 and s.loss_cooldown_minutes == 0
    assert s.conflicts() == []
