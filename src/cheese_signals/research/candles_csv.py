"""Read an intraday OHLC csv, whatever the platform decided to call the columns.

Every charting platform exports a different shape. MetaTrader writes angle
brackets and splits the timestamp across two tab-separated columns; Dukascopy
writes "Gmt time" with milliseconds; TradingView and most broker exports write
something else again. None of that is interesting, and all of it stops a
backtest before it starts, so it is handled here once rather than by editing
the file by hand.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

_TIME_ONLY = ("time",)
_STAMP = ("ts", "timestamp", "datetime", "date_time", "gmt time", "local time",
          "time (utc)", "date")
_OHLC = {
    "open": ("open", "o", "openprice"),
    "high": ("high", "h", "highprice"),
    "low": ("low", "l", "lowprice"),
    "close": ("close", "c", "closeprice", "last"),
}
_VOL = ("volume", "vol", "tickvol", "tick_volume", "realvolume")


def _norm(name: str) -> str:
    return str(name).strip().strip("<>").strip().lower().replace("_", " ")


def _parse_timestamps(raw: pd.Series) -> pd.Series:
    """Parse dates, deciding day-first vs month-first from the data itself.

    This is the one silent corruption worth guarding against: "03.08.2026"
    is 3 August to Dukascopy and 8 March to pandas' default, and picking
    wrong does not raise -- it shifts every bar into the wrong session and
    quietly rebuilds every daily pivot on the wrong day's high and low.

    So the ambiguity is resolved by evidence rather than convention: any
    first field above 12 can only be a day, any second field above 12 can
    only be a month. Only when a file is genuinely ambiguous throughout does
    the separator break the tie, and that assumption is announced.
    """
    first = raw.str.extract(r"^\s*(\d{1,4})[./-](\d{1,2})", expand=True)
    a = pd.to_numeric(first[0], errors="coerce")
    b = pd.to_numeric(first[1], errors="coerce")

    iso_like = (a > 31).any()          # leading 4-digit year: unambiguous
    day_first = bool((a > 12).any())
    month_first = bool((b > 12).any())

    if iso_like or (month_first and not day_first):
        kw = {}
    elif day_first and not month_first:
        kw = {"dayfirst": True}
    elif day_first and month_first:
        raise SystemExit("this file mixes day-first and month-first dates; "
                         "re-export it with ISO timestamps")
    else:
        # Every date <= 12/12 -- unresolvable from content alone.
        dotted = raw.str.contains(r"^\s*\d{1,2}\.\d{1,2}\.", regex=True).any()
        kw = {"dayfirst": True} if dotted else {}
        print(f"  note: dates are ambiguous (no field above 12); reading them as "
              f"{'day' if kw else 'month'}-first. Check the span printed below.")

    out = pd.to_datetime(raw, utc=True, errors="coerce", format="mixed", **kw)
    if out.isna().all() and not kw:
        out = pd.to_datetime(raw, utc=True, errors="coerce", dayfirst=True)
    return out


def load(path: str | Path) -> pd.DataFrame:
    """OHLC(V) indexed by a UTC timestamp, sorted, duplicates dropped."""
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"no such file: {path}\n"
                         f"(pass the path to your own export -- "
                         f"'your_nas100_m5.csv' was a placeholder)")

    # sep=None lets the sniffer handle comma, tab and semicolon exports.
    df = pd.read_csv(path, sep=None, engine="python")
    cols = {_norm(c): c for c in df.columns}

    # --- timestamp ---------------------------------------------------------
    if "date" in cols and "time" in cols:
        raw = df[cols["date"]].astype(str).str.strip() + " " + \
              df[cols["time"]].astype(str).str.strip()
    else:
        key = next((k for k in _STAMP if k in cols), None)
        if key is None:
            key = next((k for k in _TIME_ONLY if k in cols), None)
        if key is None:
            raise SystemExit(
                f"could not find a timestamp column in {path.name}\n"
                f"  columns present: {list(df.columns)}\n"
                f"  expected one of: ts, timestamp, datetime, date, time, "
                f"'Gmt time' -- or a Date column and a Time column")
        raw = df[cols[key]].astype(str).str.strip()

    ts = _parse_timestamps(raw)
    if ts.isna().all():
        raise SystemExit(f"could not parse timestamps; first value was {raw.iloc[0]!r}")

    # --- prices ------------------------------------------------------------
    out = {}
    for want, aliases in _OHLC.items():
        src = next((cols[a] for a in aliases if a in cols), None)
        if src is None:
            raise SystemExit(
                f"could not find a '{want}' column in {path.name}\n"
                f"  columns present: {list(df.columns)}")
        out[want] = pd.to_numeric(df[src], errors="coerce")

    vsrc = next((cols[a] for a in _VOL if a in cols), None)
    out["volume"] = pd.to_numeric(df[vsrc], errors="coerce") if vsrc else 0.0

    frame = pd.DataFrame(out)
    frame.index = ts
    frame = frame[frame.index.notna()].dropna(subset=["open", "high", "low", "close"])
    frame = frame[~frame.index.duplicated(keep="first")].sort_index()
    if frame.empty:
        raise SystemExit(f"{path.name} parsed to zero usable rows")
    return frame


def describe(df: pd.DataFrame) -> str:
    span = df.index[-1] - df.index[0]
    step = df.index.to_series().diff().dt.total_seconds().median()
    return (f"{len(df):,} bars, {df.index[0]:%Y-%m-%d %H:%M} .. "
            f"{df.index[-1]:%Y-%m-%d %H:%M} UTC ({span.days} days), "
            f"median step {step / 60:.0f} min")
