"""The csv loader, tested mostly on dates.

Column naming being wrong stops the run, which is annoying but safe. A date
being wrong does not stop anything: "03.08.2026" read as 8 March instead of
3 August shifts every bar into a different session and rebuilds every daily
pivot on the wrong day's high and low, and the backtest still prints a
confident number at the end. That is the failure worth testing.
"""

import pandas as pd
import pytest

from cheese_signals.research import candles_csv


def _w(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_metatrader_tab_separated_with_split_date_and_time(tmp_path):
    p = _w(tmp_path, "mt5.csv",
           "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\n"
           "2026.08.03\t00:00:00\t30000\t30010\t29990\t30005\t120\n"
           "2026.08.03\t00:05:00\t30005\t30020\t30000\t30015\t99\n")
    df = candles_csv.load(p)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index[0] == pd.Timestamp("2026-08-03 00:00", tz="UTC")
    assert df["close"].iloc[-1] == 30015


def test_day_first_dates_are_not_read_as_month_first(tmp_path):
    """13.08 can only be 13 August; reading it as month 13 would be silent."""
    p = _w(tmp_path, "duka.csv",
           "Gmt time,Open,High,Low,Close\n"
           "13.08.2026 00:00:00.000,30000,30010,29990,30005\n"
           "14.08.2026 00:00:00.000,30005,30020,30000,30015\n")
    df = candles_csv.load(p)
    assert df.index[0] == pd.Timestamp("2026-08-13", tz="UTC")
    assert df.index[1] == pd.Timestamp("2026-08-14", tz="UTC")


def test_month_first_dates_still_work(tmp_path):
    p = _w(tmp_path, "us.csv",
           "Date,Open,High,Low,Close\n"
           "08/13/2026 00:00,30000,30010,29990,30005\n"
           "08/14/2026 00:00,30005,30020,30000,30015\n")
    df = candles_csv.load(p)
    assert df.index[0] == pd.Timestamp("2026-08-13", tz="UTC")


def test_iso_timestamps_are_left_alone(tmp_path):
    p = _w(tmp_path, "iso.csv",
           "ts,open,high,low,close\n"
           "2026-08-03T00:00:00Z,30000,30010,29990,30005\n"
           "2026-08-03T00:05:00Z,30005,30020,30000,30015\n")
    df = candles_csv.load(p)
    assert df.index[0] == pd.Timestamp("2026-08-03 00:00", tz="UTC")


def test_a_file_mixing_both_conventions_is_refused(tmp_path):
    """13.08 and 08.13 in one file cannot both be right -- better to stop."""
    p = _w(tmp_path, "mixed.csv",
           "Gmt time,Open,High,Low,Close\n"
           "13.08.2026 00:00:00.000,30000,30010,29990,30005\n"
           "08.13.2026 00:00:00.000,30005,30020,30000,30015\n")
    with pytest.raises(SystemExit, match="mixes day-first and month-first"):
        candles_csv.load(p)


def test_rows_are_sorted_and_deduplicated(tmp_path):
    p = _w(tmp_path, "dup.csv",
           "ts,open,high,low,close\n"
           "2026-08-03T00:05:00Z,3,3,3,3\n"
           "2026-08-03T00:00:00Z,1,1,1,1\n"
           "2026-08-03T00:05:00Z,9,9,9,9\n")
    df = candles_csv.load(p)
    assert len(df) == 2
    assert df.index.is_monotonic_increasing
    assert df["close"].iloc[1] == 3


def test_missing_file_says_so_rather_than_traceback(tmp_path):
    with pytest.raises(SystemExit, match="no such file"):
        candles_csv.load(tmp_path / "nope.csv")


def test_missing_price_column_names_what_it_found(tmp_path):
    p = _w(tmp_path, "part.csv", "ts,open,high\n2026-08-03T00:00:00Z,1,2\n")
    with pytest.raises(SystemExit, match="could not find a 'low' column"):
        candles_csv.load(p)
