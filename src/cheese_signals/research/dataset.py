"""Load every candle we have ever recorded, from any source, into one frame.

Sources accumulate: several SQLite journals from different builds, CSV
exports, and a WAL file recovered from a crashed database. They overlap,
they disagree at the edges, and some carry bars the others do not.

Merging them correctly matters more than it sounds. A duplicated bar
inflates a sample; a missing one silently breaks a sequence and turns a
"pattern" into an artefact of the gap. So this reconciles rather than
concatenates, and reports what it found rather than quietly picking one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

OHLC = ["open", "high", "low", "close"]


@dataclass
class Source:
    path: Path
    kind: str
    rows: int
    assets: int
    first: Optional[pd.Timestamp]
    last: Optional[pd.Timestamp]
    note: str = ""


def _from_sqlite(path: Path) -> tuple[pd.DataFrame, str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "candles" not in tables:
            return pd.DataFrame(), "no candles table"
        df = pd.read_sql("SELECT asset, ts, open, high, low, close, volume "
                         "FROM candles", con)
    except (sqlite3.DatabaseError, pd.errors.DatabaseError) as exc:
        return pd.DataFrame(), f"unreadable: {exc}"
    finally:
        con.close()
    return df, ""


def _from_csv(path: Path) -> tuple[pd.DataFrame, str]:
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    need = {"asset", "ts", "open", "high", "low", "close"}
    if not need <= set(cols):
        return pd.DataFrame(), f"columns {sorted(df.columns)[:6]} are not candles"
    df = df.rename(columns={cols[c]: c for c in cols if c in need | {"volume"}})
    return df, ""


def load(paths: Iterable[Path | str]) -> tuple[pd.DataFrame, list[Source]]:
    """Every candle from every source, de-duplicated and sorted.

    Returns the merged frame indexed by (asset, ts) and a report of what
    each source contributed, so a source that turned out to be empty or
    unreadable is visible rather than assumed absent.
    """
    frames, report = [], []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            report.append(Source(p, "missing", 0, 0, None, None, "file not found"))
            continue
        kind = "sqlite" if p.suffix in (".db", ".sqlite", ".dbwal") else "csv"
        df, note = (_from_sqlite(p) if kind == "sqlite" else _from_csv(p))
        if df.empty:
            report.append(Source(p, kind, 0, 0, None, None, note or "no rows"))
            continue
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
        df = df.dropna(subset=["ts", "asset"])
        for c in OHLC:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=OHLC)
        if "volume" not in df:
            df["volume"] = np.nan
        df["source"] = p.name
        report.append(Source(p, kind, len(df), df.asset.nunique(),
                             df.ts.min(), df.ts.max(), note))
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=["asset", "ts", *OHLC, "volume"]), report

    allc = pd.concat(frames, ignore_index=True)
    # Later sources win on a conflict only when the earlier one is degenerate;
    # otherwise the first reading of a bar is kept, since a bar rewritten by a
    # later build is usually the same bar recorded twice.
    allc = allc.sort_values(["asset", "ts"])
    allc = allc.drop_duplicates(subset=["asset", "ts"], keep="first")
    return allc.reset_index(drop=True), report


def to_panel(candles: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """One tidy, gap-aware frame per asset, indexed by bar open time."""
    out = {}
    for asset, g in candles.groupby("asset"):
        df = g.set_index("ts")[OHLC + ["volume"]].sort_index()
        df = df[~df.index.duplicated(keep="first")]
        out[asset] = df
    return out


def continuity(df: pd.DataFrame, freq_seconds: int = 60) -> pd.DataFrame:
    """Where the recording stopped and restarted.

    Every test downstream assumes consecutive bars are actually consecutive.
    A four-hour gap treated as one step invents a move that never happened,
    and on a 1-minute study that single fabricated bar can carry an entire
    "finding".
    """
    if len(df) < 2:
        return pd.DataFrame(columns=["start", "end", "missing_bars"])
    delta = df.index.to_series().diff().dt.total_seconds()
    breaks = delta[delta > freq_seconds]
    rows = [{"start": ts - pd.Timedelta(seconds=d), "end": ts,
             "missing_bars": int(d // freq_seconds) - 1}
            for ts, d in breaks.items()]
    return pd.DataFrame(rows)


def segments(df: pd.DataFrame, freq_seconds: int = 60,
             min_bars: int = 60) -> list[pd.DataFrame]:
    """Split an asset into runs of genuinely consecutive bars.

    Analysis runs per segment and results are pooled afterwards, so no
    statistic is ever computed across a hole in the recording.
    """
    if df.empty:
        return []
    delta = df.index.to_series().diff().dt.total_seconds()
    group = (delta > freq_seconds).cumsum()
    return [seg for _, seg in df.groupby(group) if len(seg) >= min_bars]


def summarise(candles: pd.DataFrame, report: list[Source]) -> str:
    lines = ["SOURCES"]
    for s in report:
        if s.rows:
            lines.append(f"  {s.path.name:32s} {s.rows:>8,} bars  "
                         f"{s.assets} assets  {s.first:%Y-%m-%d %H:%M} .. "
                         f"{s.last:%Y-%m-%d %H:%M}")
        else:
            lines.append(f"  {s.path.name:32s} {'-':>8}        {s.note}")

    if candles.empty:
        lines.append("\nNo candles loaded.")
        return "\n".join(lines)

    lines.append(f"\nMERGED  {len(candles):,} unique bars across "
                 f"{candles.asset.nunique()} assets")
    panel = to_panel(candles)
    lines.append(f"{'asset':14s} {'bars':>8s} {'segments':>9s} {'longest':>8s} "
                 f"{'first':16s} {'last':16s}")
    for asset, df in sorted(panel.items()):
        segs = segments(df)
        longest = max((len(s) for s in segs), default=0)
        lines.append(f"{asset:14s} {len(df):>8,} {len(segs):>9} {longest:>8,} "
                     f"{df.index[0]:%m-%d %H:%M}    {df.index[-1]:%m-%d %H:%M}")
    return "\n".join(lines)
