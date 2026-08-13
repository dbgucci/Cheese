"""Pull every candle this project has ever recorded into one place.

Sources, in the order they are usually available:

``signals.db``  the live journal, written by the engine and the autotrader
                every minute they run. This is the real archive -- it is your
                broker's own OTC feed as your account saw it, which is the
                only feed the options actually settle against.
``*.csv``       exported or hand-collected history, for anything gathered
                before the journal existed or pulled from another tool.

The journal lives in ``<Desktop>/KPS/signals.db`` and is gitignored on
purpose (it holds a trade history, not source code), so it never travels
with a clone of the repository. Research therefore runs where the data is:
on the machine that ran the bots.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import pandas as pd

from .. import paths

OHLC = ["open", "high", "low", "close"]


@dataclass
class AssetHistory:
    """One asset's candles, plus where they came from."""

    asset: str
    candles: pd.DataFrame
    source: str

    @property
    def n_bars(self) -> int:
        return len(self.candles)

    @property
    def span(self) -> str:
        if self.candles.empty:
            return "empty"
        start, end = self.candles.index[0], self.candles.index[-1]
        return f"{start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC"

    @property
    def coverage(self) -> float:
        """Fraction of the calendar minutes in the span that are present.

        A low number is not a bug: OTC pairs trade continuously but the bot
        only records while it is running, so gaps are periods nobody was
        watching. It matters for research because a "pattern" that spans a
        gap is comparing two unrelated moments.
        """
        if len(self.candles) < 2:
            return 0.0
        minutes = (self.candles.index[-1] - self.candles.index[0]).total_seconds() / 60.0
        return len(self.candles) / minutes if minutes > 0 else 0.0


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce to numeric OHLCV, drop broken rows, sort, de-duplicate."""
    if df.empty:
        return df

    out = df.copy()
    for col in OHLC + ["volume"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out.dropna(subset=OHLC)

    # A candle whose high is below its low (or below a traded price) is
    # corrupt; keeping it would poison every range-based feature.
    valid = (
        (out["high"] >= out["low"])
        & (out["high"] >= out["open"])
        & (out["high"] >= out["close"])
        & (out["low"] <= out["open"])
        & (out["low"] <= out["close"])
        & (out["close"] > 0)
    )
    out = out[valid]

    if "volume" not in out.columns:
        out["volume"] = 0.0

    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def from_journal(
    db_path: Optional[Path] = None, min_bars: int = 200
) -> Iterator[AssetHistory]:
    """Yield every asset in the journal that has enough candles to study."""
    path = Path(db_path) if db_path else paths.db_path()
    if not path.exists():
        return

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            assets = [
                r[0]
                for r in conn.execute(
                    "SELECT asset, COUNT(*) c FROM candles GROUP BY asset "
                    "HAVING c >= ? ORDER BY c DESC",
                    (min_bars,),
                )
            ]
        except sqlite3.DatabaseError:
            return  # not a journal, or schema predates the candles table

        for asset in assets:
            rows = conn.execute(
                "SELECT ts, open, high, low, close, volume FROM candles "
                "WHERE asset = ? ORDER BY ts",
                (asset,),
            ).fetchall()
            if not rows:
                continue
            df = pd.DataFrame([dict(r) for r in rows])
            df.index = pd.to_datetime(df.pop("ts"), utc=True, format="mixed")
            df = _normalize(df)
            if len(df) >= min_bars:
                yield AssetHistory(asset=asset, candles=df, source=str(path))
    finally:
        conn.close()


def from_csv_dir(directory: Path, min_bars: int = 200) -> Iterator[AssetHistory]:
    """Yield candles from every CSV in a directory.

    Accepts the shape ``csv_feed.CsvFeed`` reads (a ``timestamp`` column plus
    OHLC), and also the common export shapes: ``time``/``date``/``datetime``
    as the index column, with or without volume. The asset name is taken from
    the filename.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return

    for path in sorted(directory.glob("*.csv")):
        try:
            df = pd.read_csv(path)
        except (OSError, pd.errors.ParserError, UnicodeDecodeError):
            continue
        if df.empty:
            continue

        lowered = {c.lower().strip(): c for c in df.columns}
        ts_col = next(
            (
                lowered[name]
                for name in ("timestamp", "time", "date", "datetime", "open_time")
                if name in lowered
            ),
            None,
        )
        if ts_col is None:
            continue

        df = df.rename(columns={v: k for k, v in lowered.items()})
        ts_key = ts_col.lower().strip()
        raw = df.pop(ts_key)

        # Epoch seconds and epoch milliseconds are both common in exports.
        index = None
        if pd.api.types.is_numeric_dtype(raw):
            unit = "ms" if float(raw.max()) > 1e11 else "s"
            index = pd.to_datetime(raw, unit=unit, utc=True, errors="coerce")
        if index is None or index.isna().all():
            index = pd.to_datetime(raw, utc=True, errors="coerce")

        df.index = index
        df = df[df.index.notna()]
        if not all(c in df.columns for c in OHLC):
            continue

        df = _normalize(df)
        if len(df) >= min_bars:
            yield AssetHistory(asset=path.stem, candles=df, source=str(path))


def collect(
    db_path: Optional[Path] = None,
    csv_dir: Optional[Path] = None,
    min_bars: int = 200,
) -> list[AssetHistory]:
    """Gather every source into one list, merging assets that appear twice."""
    merged: dict[str, AssetHistory] = {}

    sources: list[Iterator[AssetHistory]] = [from_journal(db_path, min_bars=1)]
    if csv_dir:
        sources.append(from_csv_dir(csv_dir, min_bars=1))

    for stream in sources:
        for history in stream:
            existing = merged.get(history.asset)
            if existing is None:
                merged[history.asset] = history
                continue
            combined = pd.concat([existing.candles, history.candles])
            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
            merged[history.asset] = AssetHistory(
                asset=history.asset,
                candles=combined,
                source=f"{existing.source} + {history.source}",
            )

    return sorted(
        (h for h in merged.values() if h.n_bars >= min_bars),
        key=lambda h: h.n_bars,
        reverse=True,
    )


def split_sessions(df: pd.DataFrame, max_gap_minutes: int = 5) -> list[pd.DataFrame]:
    """Break a frame into contiguous runs, splitting wherever recording stopped.

    Every feature and every outcome in this project is defined over *adjacent*
    candles. Across a recording gap, "the next candle" might be nine hours
    later, and a rule scored on that pairing is measuring nothing. Splitting
    first is what keeps the mining honest.
    """
    if df.empty:
        return []

    gaps = df.index.to_series().diff()
    boundaries = gaps > pd.Timedelta(minutes=max_gap_minutes)
    group_id = boundaries.cumsum()

    return [chunk for _, chunk in df.groupby(group_id) if len(chunk) > 1]
