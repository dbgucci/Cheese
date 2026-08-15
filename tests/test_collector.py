"""The collector: six pairs, an editable list, and weeks of unattended running.

The failure that matters is not a crash. It is a process that appears to be
running for three weeks and has been storing nothing since the second night,
so the tests are mostly about what happens when things go wrong.
"""

from pathlib import Path

import pandas as pd
import pytest

from cheese_signals.research import collect
from cheese_signals.research.collect import Collector, CollectorConfig
from cheese_signals.storage import Journal


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("KPS_COLLECT_HOME", str(tmp_path))
    return tmp_path


def _bars(n=5, start="2026-08-16 00:00", price=1.1):
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({"open": price, "high": price + 0.001, "low": price - 0.001,
                         "close": price, "volume": 100.0}, index=idx)


class _Feed:
    def __init__(self, asset, frames=None, fail=0):
        self.asset = asset
        self.calls = 0
        self.fail = fail
        self.frames = frames

    def get_candles(self, count):
        self.calls += 1
        if self.calls <= self.fail:
            raise ConnectionError("socket closed")
        if self.frames is not None:
            return self.frames
        return _bars(start=f"2026-08-16 {self.calls - 1:02d}:00")


def _collector(tmp_path, cfg=None, factory=None):
    cfg = cfg or CollectorConfig(assets=["EURUSD_otc", "GBPUSD_otc"])
    j = Journal(tmp_path / "c.db")
    made = {}

    def default(asset):
        made.setdefault(asset, _Feed(asset))
        return made[asset]

    c = Collector(cfg, j, factory or default, on_log=lambda m: None)
    return c, j, made


# ------------------------------ the pair list ------------------------------
def test_all_six_otc_majors_are_collected_by_default(_home):
    cfg = CollectorConfig.load()
    assert cfg.assets == ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc",
                          "AUDUSD_otc", "USDCAD_otc", "EURJPY_otc"]


def test_a_pairs_file_is_written_on_first_run_and_is_editable(_home):
    CollectorConfig.load()
    p = _home / "pairs.txt"
    assert p.exists()
    body = p.read_text()
    assert "EURUSD_otc" in body and body.lstrip().startswith("#")


def test_editing_the_pairs_file_changes_what_is_collected(_home):
    CollectorConfig.load()
    (_home / "pairs.txt").write_text("# mine\nGBPJPY_otc\nNZDUSD_otc\n")
    assert CollectorConfig.load().assets == ["GBPJPY_otc", "NZDUSD_otc"]


def test_a_pair_list_on_one_line_still_parses(_home):
    """The mistake that once turned a whole watchlist into a single symbol."""
    CollectorConfig.load()
    (_home / "pairs.txt").write_text("EURUSD_otc, GBPUSD_otc USDJPY_otc")
    assert CollectorConfig.load().assets == ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc"]


def test_comments_and_blank_lines_are_ignored(_home):
    CollectorConfig.load()
    (_home / "pairs.txt").write_text("# header\n\nEURUSD_otc\n\n# NZDUSD_otc\n")
    assert CollectorConfig.load().assets == ["EURUSD_otc"]


def test_an_empty_pairs_file_falls_back_to_the_defaults(_home):
    """Better six pairs than a collector that silently records nothing."""
    CollectorConfig.load()
    (_home / "pairs.txt").write_text("# everything commented out\n")
    assert len(CollectorConfig.load().assets) == 6


def test_a_corrupt_config_does_not_stop_the_collector(_home):
    (_home / "collector.json").write_text("{ not json")
    assert CollectorConfig.load().assets


# ------------------------------ collecting ------------------------------
def test_every_configured_pair_is_pulled_each_cycle(tmp_path):
    c, j, made = _collector(tmp_path)
    c.cycle()
    assert set(made) == {"EURUSD_otc", "GBPUSD_otc"}
    assert all(f.calls == 1 for f in made.values())
    j.close()


def test_bars_reach_the_database(tmp_path):
    c, j, _ = _collector(tmp_path)
    c.cycle()
    counts = j.candle_counts()
    assert counts["EURUSD_otc"] == 5 and counts["GBPUSD_otc"] == 5
    j.close()


def test_re_reading_the_same_tail_does_not_duplicate_rows(tmp_path):
    """A restart re-reads history; storing it twice would inflate the study."""
    frames = _bars(10)
    c, j, _ = _collector(tmp_path, factory=lambda a: _Feed(a, frames=frames))
    c.cycle(); c.cycle(); c.cycle()
    assert j.candle_counts()["EURUSD_otc"] == 10
    j.close()


# ------------------------------ staying up ------------------------------
def test_one_pair_failing_does_not_stop_the_others(tmp_path):
    def factory(asset):
        return _Feed(asset, fail=99 if asset == "GBPUSD_otc" else 0)

    c, j, _ = _collector(tmp_path, factory=factory)
    c.cycle()
    counts = j.candle_counts()
    assert counts.get("EURUSD_otc") == 5
    assert "GBPUSD_otc" not in counts
    assert c.state["GBPUSD_otc"].consecutive_failures == 1
    j.close()


def test_a_failing_feed_is_rebuilt_rather_than_left_dead(tmp_path):
    """A closed socket stays closed; only a fresh feed reconnects."""
    built = []

    def factory(asset):
        f = _Feed(asset, fail=99)
        built.append(f)
        return f

    c, j, _ = _collector(tmp_path, CollectorConfig(assets=["EURUSD_otc"]), factory)
    # The feed is discarded on the third failure and rebuilt on the next pull.
    for _ in range(4):
        c.cycle()
    assert len(built) >= 2, "the feed was never rebuilt after repeated failures"
    j.close()


def test_a_recovered_feed_clears_its_failure_count(tmp_path):
    c, j, _ = _collector(tmp_path, CollectorConfig(assets=["EURUSD_otc"]),
                         lambda a: _Feed(a, fail=1))
    c.cycle()
    assert c.state["EURUSD_otc"].consecutive_failures == 1
    c.cycle()
    assert c.state["EURUSD_otc"].consecutive_failures == 0
    j.close()


def test_an_empty_response_is_recorded_as_a_failure_not_a_success(tmp_path):
    c, j, _ = _collector(tmp_path, CollectorConfig(assets=["EURUSD_otc"]),
                         lambda a: _Feed(a, frames=pd.DataFrame()))
    c.cycle()
    st = c.state["EURUSD_otc"]
    assert st.consecutive_failures == 1 and "no candles" in st.last_error
    j.close()


def test_the_loop_stops_when_asked(tmp_path):
    c, j, _ = _collector(tmp_path)
    c.run(max_cycles=3, sleeper=lambda s: None)
    assert c.cycles == 3
    j.close()


# ------------------------------ knowing where you are ------------------------------
def test_progress_reports_against_the_sample_the_study_needs(tmp_path):
    c, j, _ = _collector(tmp_path)
    c.cycle()
    text = c.progress()
    assert f"{collect.TARGET_BARS:,}" in text
    assert "EURUSD_otc" in text and "GBPUSD_otc" in text
    j.close()


def test_progress_counts_what_is_on_disk_not_what_was_written(tmp_path):
    """So the number survives a restart and matches the file that gets sent."""
    c, j, _ = _collector(tmp_path)
    c.cycle()
    c2 = Collector(c.cfg, j, lambda a: _Feed(a), on_log=lambda m: None)
    total, per = c2.totals()
    assert total == 10 and per["EURUSD_otc"] == 5
    j.close()


def test_a_pairs_error_is_visible_in_the_progress_report(tmp_path):
    c, j, _ = _collector(tmp_path, CollectorConfig(assets=["EURUSD_otc"]),
                         lambda a: _Feed(a, fail=99))
    c.cycle()
    assert "socket closed" in c.progress()
    j.close()


def test_the_study_can_read_what_the_collector_writes(tmp_path):
    """The whole point: the output file feeds straight into the analysis."""
    from cheese_signals.research import dataset

    c, j, _ = _collector(tmp_path)
    c.cycle()
    j.close()
    candles, report = dataset.load([tmp_path / "c.db"])
    assert len(candles) == 10
    assert set(candles.asset) == {"EURUSD_otc", "GBPUSD_otc"}
    assert report[0].rows == 10


# ------------------------------ setup safety ------------------------------
def test_a_missing_session_says_what_to_do_instead_of_failing_obscurely(_home):
    cfg = CollectorConfig(ssid="", data_source="pocket_option")
    with pytest.raises(SystemExit) as exc:
        collect.build_feed_factory(cfg)
    assert "ssid" in str(exc.value) and "synthetic" in str(exc.value)


def test_the_synthetic_source_needs_no_session(_home):
    factory = collect.build_feed_factory(
        CollectorConfig(ssid="", data_source="synthetic"))
    feed = factory("EURUSD_otc")
    assert len(feed.get_candles(20)) > 0
