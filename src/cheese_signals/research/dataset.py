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
class Skipped:
    """A source that could not be used, and why.

    Reported rather than swallowed: a run that quietly ignores half the data
    still prints a confident-looking report, and the reader has no way to
    tell. Every skip shows up in the inventory.
    """

    path: str
    reason: str


@dataclass
class Collection:
    """Everything ``collect`` found, plus everything it could not use."""

    histories: list["AssetHistory"]
    skipped: list[Skipped]
    sources_read: list[str]

    def __iter__(self):
        return iter(self.histories)

    def __len__(self) -> int:
        return len(self.histories)

    def __bool__(self) -> bool:
        return bool(self.histories)

    @property
    def total_bars(self) -> int:
        return sum(h.n_bars for h in self.histories)


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
    for history in _read_journal(db_path, min_bars, skipped=[]):
        yield history


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]


def _candle_table(conn: sqlite3.Connection) -> Optional[str]:
    """Find a table that looks like OHLC candles, whatever it happens to be called.

    Other bots in this family write their own databases with their own
    schemas. Rather than reading only ``candles`` and silently ignoring
    everything else, look for any table carrying open/high/low/close.
    """
    for table in _table_names(conn):
        try:
            cols = {
                str(r[1]).lower()
                for r in conn.execute(f'PRAGMA table_info("{table}")')
            }
        except sqlite3.DatabaseError:
            continue
        if {"open", "high", "low", "close"} <= cols:
            return table
    return None


def _column_map(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    cols = [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')]
    lowered = {c.lower(): c for c in cols}

    mapping: dict[str, str] = {}
    for want in OHLC + ["volume"]:
        if want in lowered:
            mapping[want] = lowered[want]

    for candidate in ("ts", "timestamp", "time", "datetime", "date", "open_time"):
        if candidate in lowered:
            mapping["ts"] = lowered[candidate]
            break

    for candidate in ("asset", "symbol", "pair", "instrument"):
        if candidate in lowered:
            mapping["asset"] = lowered[candidate]
            break

    return mapping


def _read_journal(
    db_path: Optional[Path],
    min_bars: int,
    skipped: list["Skipped"],
) -> Iterator[AssetHistory]:
    """Read candles from one SQLite file, recording why if it cannot."""
    path = Path(db_path) if db_path else paths.db_path()
    if not path.exists():
        skipped.append(Skipped(str(path), "file does not exist"))
        return

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        skipped.append(Skipped(str(path), f"could not open: {exc}"))
        return

    conn.row_factory = sqlite3.Row
    try:
        try:
            table = _candle_table(conn)
            tables = _table_names(conn)
        except sqlite3.DatabaseError as exc:
            skipped.append(Skipped(str(path), f"unreadable: {exc}"))
            return

        if table is None:
            skipped.append(
                Skipped(
                    str(path),
                    "no table with open/high/low/close columns "
                    f"(tables present: {', '.join(tables) or 'none'})",
                )
            )
            return

        mapping = _column_map(conn, table)
        if "ts" not in mapping:
            skipped.append(
                Skipped(str(path), f"table '{table}' has no timestamp column")
            )
            return

        select = [f'"{mapping[c]}" AS "{c}"' for c in OHLC if c in mapping]
        select.append(f'"{mapping["ts"]}" AS ts')
        if "volume" in mapping:
            select.append(f'"{mapping["volume"]}" AS volume')

        has_asset = "asset" in mapping
        if has_asset:
            select.append(f'"{mapping["asset"]}" AS asset')

        try:
            rows = conn.execute(
                f'SELECT {", ".join(select)} FROM "{table}"'
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            skipped.append(Skipped(str(path), f"query failed: {exc}"))
            return

        if not rows:
            skipped.append(Skipped(str(path), f"table '{table}' is empty"))
            return

        frame = pd.DataFrame([dict(r) for r in rows])
        frame.index = pd.to_datetime(
            frame.pop("ts"), utc=True, format="mixed", errors="coerce"
        )
        frame = frame[frame.index.notna()]

        # A single-asset table (one bot, one pair) has no asset column; name
        # it after the file so it is still identifiable in the report.
        if not has_asset:
            frame["asset"] = path.stem

        found_any = False
        for asset, group in frame.groupby("asset"):
            df = _normalize(group.drop(columns=["asset"]))
            if len(df) >= min_bars:
                found_any = True
                yield AssetHistory(asset=str(asset), candles=df, source=str(path))

        if not found_any:
            skipped.append(
                Skipped(
                    str(path),
                    f"table '{table}' has no asset with >= {min_bars:,} usable candles",
                )
            )
    finally:
        conn.close()


def from_csv_dir(
    directory: Path,
    min_bars: int = 200,
    recursive: bool = True,
    skipped: Optional[list["Skipped"]] = None,
) -> Iterator[AssetHistory]:
    """Yield candles from every CSV under a directory.

    Accepts the shape ``csv_feed.CsvFeed`` reads (a ``timestamp`` column plus
    OHLC), and also the common export shapes: ``time``/``date``/``datetime``
    as the index column, with or without volume. The asset name is taken from
    the filename.

    Anything that is not candle data -- a trade log, a results table -- is
    recorded in ``skipped`` with the reason, rather than dropped in silence.
    Silence is how you end up analysing a third of your data and believing
    it was all of it.
    """
    notes = skipped if skipped is not None else []
    directory = Path(directory)
    if not directory.is_dir():
        notes.append(Skipped(str(directory), "not a directory"))
        return

    pattern = "**/*.csv" if recursive else "*.csv"
    for path in sorted(directory.glob(pattern)):
        try:
            df = pd.read_csv(path)
        except (OSError, pd.errors.ParserError, UnicodeDecodeError) as exc:
            notes.append(Skipped(str(path), f"unreadable: {type(exc).__name__}"))
            continue
        if df.empty:
            notes.append(Skipped(str(path), "empty file"))
            continue

        lowered = {c.lower().strip(): c for c in df.columns}
        ts_key = next(
            (
                name
                for name in ("timestamp", "time", "date", "datetime", "open_time")
                if name in lowered
            ),
            None,
        )
        if ts_key is None:
            notes.append(
                Skipped(
                    str(path),
                    f"no timestamp column (has: {', '.join(list(df.columns)[:6])})",
                )
            )
            continue

        df = df.rename(columns={v: k for k, v in lowered.items()})
        raw = df.pop(ts_key)

        # Epoch seconds and epoch milliseconds are both common in exports.
        index = None
        if pd.api.types.is_numeric_dtype(raw):
            unit = "ms" if float(pd.to_numeric(raw, errors="coerce").max()) > 1e11 else "s"
            index = pd.to_datetime(raw, unit=unit, utc=True, errors="coerce")
        if index is None or index.isna().all():
            index = pd.to_datetime(raw, utc=True, errors="coerce", format="mixed")

        df.index = index
        df = df[df.index.notna()]

        missing = [c for c in OHLC if c not in df.columns]
        if missing:
            notes.append(
                Skipped(str(path), f"not candle data -- no {', '.join(missing)} column")
            )
            continue

        df = _normalize(df)
        if len(df) < min_bars:
            notes.append(
                Skipped(str(path), f"only {len(df):,} usable candles (need {min_bars:,})")
            )
            continue

        yield AssetHistory(asset=path.stem, candles=df, source=str(path))


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [value]
    return list(value)


def collect(
    db_path=None,
    csv_dir=None,
    min_bars: int = 200,
) -> "Collection":
    """Gather every source into one set of assets, merging duplicates.

    ``db_path`` and ``csv_dir`` each accept a single path or a list of them,
    because the candle history for this project is spread across several
    bots' data folders rather than sitting in one file.
    """
    merged: dict[str, AssetHistory] = {}
    skipped: list[Skipped] = []
    read: list[str] = []

    db_paths = _as_list(db_path) or [None]
    csv_dirs = _as_list(csv_dir)

    streams: list[Iterator[AssetHistory]] = [
        _read_journal(Path(p) if p else None, 1, skipped) for p in db_paths
    ]
    streams += [
        from_csv_dir(Path(d), min_bars=1, skipped=skipped) for d in csv_dirs
    ]

    for stream in streams:
        for history in stream:
            if history.source not in read:
                read.append(history.source)

            existing = merged.get(history.asset)
            if existing is None:
                merged[history.asset] = history
                continue

            # The same pair recorded by two bots: union the candles, keeping
            # one row per timestamp.
            combined = pd.concat([existing.candles, history.candles])
            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
            sources = existing.source
            if history.source not in sources:
                sources = f"{sources} + {history.source}"
            merged[history.asset] = AssetHistory(
                asset=history.asset, candles=combined, source=sources
            )

    for history in merged.values():
        if history.n_bars < min_bars:
            skipped.append(
                Skipped(
                    history.asset,
                    f"only {history.n_bars:,} candles after merge (need {min_bars:,})",
                )
            )

    histories = sorted(
        (h for h in merged.values() if h.n_bars >= min_bars),
        key=lambda h: h.n_bars,
        reverse=True,
    )
    return Collection(histories=histories, skipped=skipped, sources_read=read)


# Folders that never hold trading data and can be large; skipping them keeps
# a Desktop-wide scan to a couple of seconds.
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages",
    "build", "dist", ".pytest_cache", ".mypy_cache", "AppData",
}


def discover(roots: Optional[list[Path]] = None) -> tuple[list[Path], list[Path]]:
    """Find every candle source under the user's Desktop folders.

    Returns ``(database_paths, csv_directories)``. The bots in this family
    write to several different folders -- the KPS journal, per-bot ``data``
    directories, ad-hoc research exports -- and hand-maintaining that list
    goes stale the moment another bot is added. Everything found is passed
    to the loaders, which report whatever turns out not to be candle data.
    """
    if roots is None:
        home = Path.home()
        roots = [
            home / "Desktop",
            home / "OneDrive" / "Desktop",
            home / "OneDrive - Personal" / "Desktop",
        ]

    databases: list[Path] = []
    csv_dirs: list[Path] = []
    seen: set[Path] = set()

    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                if path.is_dir():
                    continue
                suffix = path.suffix.lower()
                if suffix in (".db", ".sqlite", ".sqlite3"):
                    resolved = path.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        databases.append(path)
                elif suffix == ".csv":
                    parent = path.parent.resolve()
                    if parent not in seen:
                        seen.add(parent)
                        csv_dirs.append(path.parent)
            except OSError:
                continue  # permission denied, broken link, cloud-only file

    return databases, csv_dirs


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
