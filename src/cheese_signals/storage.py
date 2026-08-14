"""SQLite journal: every candle, every signal, every outcome, and why.

This is the part that lets the bot get better over time rather than just
firing and forgetting. Three tables:

``candles``   raw OHLCV, so a past signal can be re-simulated later against
              exactly the data the engine saw at the time (and so you can
              re-backtest new strategy ideas on your own broker's real feed
              instead of on synthetic data).
``signals``   every signal issued, including the ones that were later
              cancelled -- with the full feature snapshot (score, regime,
              ADX, session, bias, lead time) that produced it.
``outcomes``  the result at expiry plus an attributed reason, joined back to
              the signal so the analytics tab can answer "which conditions
              actually make me money on this feed".

Stored in the Desktop data folder so it's trivially findable and backup-able.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from . import paths
from . import __version__

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    asset       TEXT NOT NULL,
    ts          TEXT NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL,
    PRIMARY KEY (asset, ts)
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset           TEXT NOT NULL,
    direction       INTEGER NOT NULL,
    score           REAL NOT NULL,
    strategy        TEXT,
    reason          TEXT,
    detected_at     TEXT NOT NULL,
    entry_at        TEXT NOT NULL,
    expiry_at       TEXT NOT NULL,
    lead_seconds    INTEGER NOT NULL,
    session         TEXT,
    utc_hour        INTEGER,
    features        TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    cancel_reason   TEXT,
    app_version     TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
    signal_id       INTEGER PRIMARY KEY REFERENCES signals(id),
    asset           TEXT NOT NULL,
    direction       INTEGER NOT NULL,
    entry_price     REAL NOT NULL,
    exit_price      REAL NOT NULL,
    won             INTEGER NOT NULL,
    payout          REAL NOT NULL,
    pnl             REAL NOT NULL,
    stake           REAL NOT NULL,
    settled_at      TEXT NOT NULL,
    reason          TEXT
);

"""

# Indexes are applied *after* migrations, because an index on a column that a
# migration is about to add would fail on an existing database.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_signals_entry ON signals(entry_at);
CREATE INDEX IF NOT EXISTS idx_signals_version ON signals(app_version);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_candles_asset_ts ON candles(asset, ts);
"""


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class SignalRecord:
    id: int
    asset: str
    direction: int
    score: float
    strategy: str
    reason: str
    detected_at: str
    entry_at: str
    expiry_at: str
    lead_seconds: int
    session: str
    utc_hour: int
    features: dict[str, Any]
    status: str
    cancel_reason: Optional[str]


class Journal:
    """Thread-safe-enough SQLite wrapper (one connection, serialized writes)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else paths.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps readers from blocking on writers and is markedly faster
        # for the steady trickle of small writes this app makes. NORMAL sync
        # is the standard pairing: durable across app crashes, and the only
        # exposure is an OS-level crash losing the last transaction, which for
        # a trade journal is an acceptable trade for not stalling the UI.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.executescript(INDEXES)
        self._conn.commit()
        self._last_candle_ts: dict[str, str] = {}

    def _migrate(self) -> None:
        """Add columns to databases created by an older build.

        CREATE TABLE IF NOT EXISTS silently leaves an existing table alone, so
        a user upgrading in place keeps their old schema and their history.
        Never drop or rewrite their data -- past trades stay queryable, they
        just carry no version stamp, which is exactly how they are reported.
        """
        existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(signals)")}
        if "app_version" not in existing:
            self._conn.execute("ALTER TABLE signals ADD COLUMN app_version TEXT")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ----------------------------- candles ------------------------------
    def record_candles(self, asset: str, df) -> int:
        """Upsert candles. Returns the number of rows written.

        Only candles newer than the newest already stored are written. The
        engine hands over a 50-candle tail every minute, so writing all of it
        re-wrote 49 unchanged rows per asset per minute -- roughly fifty times
        the necessary write volume, for nothing.
        """
        if df is None or len(df) == 0:
            return 0

        watermark = self._last_candle_ts.get(asset)
        if watermark is None:
            got = self._conn.execute(
                "SELECT MAX(ts) FROM candles WHERE asset = ?", (asset,)
            ).fetchone()[0]
            watermark = got or ""
            self._last_candle_ts[asset] = watermark

        rows = [
            (
                asset,
                _iso(ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r.get("volume", 0.0) or 0.0),
            )
            for ts, r in df.iterrows()
        ]
        rows = [r for r in rows if r[1] > watermark]
        if not rows:
            return 0
        self._last_candle_ts[asset] = max(r[1] for r in rows)
        with self._tx() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO candles(asset, ts, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def candle_count(self, asset: Optional[str] = None) -> int:
        if asset:
            cur = self._conn.execute("SELECT COUNT(*) FROM candles WHERE asset = ?", (asset,))
        else:
            cur = self._conn.execute("SELECT COUNT(*) FROM candles")
        return int(cur.fetchone()[0])

    def candle_counts_by_asset(self) -> dict[str, int]:
        """Bars held per asset. Used to report how much history exists yet."""
        cur = self._conn.execute(
            "SELECT asset, COUNT(*) FROM candles GROUP BY asset"
        )
        return {str(row[0]): int(row[1]) for row in cur.fetchall()}

    def load_candles(self, asset: str, limit: int = 5000):
        import pandas as pd

        cur = self._conn.execute(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE asset = ? ORDER BY ts DESC LIMIT ?",
            (asset, limit),
        )
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame([dict(r) for r in rows])
        df.index = pd.to_datetime(df.pop("ts"), utc=True)
        return df.sort_index()

    # ----------------------------- signals ------------------------------
    def record_signal(
        self,
        asset: str,
        direction: int,
        score: float,
        strategy: str,
        reason: str,
        detected_at: datetime,
        entry_at: datetime,
        expiry_at: datetime,
        session: str,
        utc_hour: int,
        features: dict[str, Any],
    ) -> int:
        lead_seconds = int((entry_at - detected_at).total_seconds())
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO signals(asset, direction, score, strategy, reason, detected_at, "
                "entry_at, expiry_at, lead_seconds, session, utc_hour, features, status, "
                "app_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                (
                    asset,
                    direction,
                    score,
                    strategy,
                    reason,
                    _iso(detected_at),
                    _iso(entry_at),
                    _iso(expiry_at),
                    lead_seconds,
                    session,
                    utc_hour,
                    json.dumps(features),
                    __version__,
                ),
            )
            return int(cur.lastrowid)

    def set_signal_status(self, signal_id: int, status: str, cancel_reason: Optional[str] = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE signals SET status = ?, cancel_reason = ? WHERE id = ?",
                (status, cancel_reason, signal_id),
            )

    def pending_signals(self) -> list[SignalRecord]:
        cur = self._conn.execute(
            "SELECT * FROM signals WHERE status IN ('pending', 'active') ORDER BY entry_at"
        )
        return [self._to_signal(r) for r in cur.fetchall()]

    def recent_signals(self, limit: int = 100) -> list[SignalRecord]:
        cur = self._conn.execute(
            "SELECT * FROM signals ORDER BY detected_at DESC LIMIT ?", (limit,)
        )
        return [self._to_signal(r) for r in cur.fetchall()]

    @staticmethod
    def _to_signal(r: sqlite3.Row) -> SignalRecord:
        return SignalRecord(
            id=r["id"],
            asset=r["asset"],
            direction=r["direction"],
            score=r["score"],
            strategy=r["strategy"] or "",
            reason=r["reason"] or "",
            detected_at=r["detected_at"],
            entry_at=r["entry_at"],
            expiry_at=r["expiry_at"],
            lead_seconds=r["lead_seconds"],
            session=r["session"] or "",
            utc_hour=r["utc_hour"] if r["utc_hour"] is not None else -1,
            features=json.loads(r["features"] or "{}"),
            status=r["status"],
            cancel_reason=r["cancel_reason"],
        )

    # ----------------------------- outcomes -----------------------------
    def record_outcome(
        self,
        signal_id: int,
        asset: str,
        direction: int,
        entry_price: float,
        exit_price: float,
        won: bool,
        payout: float,
        stake: float,
        settled_at: datetime,
        reason: str,
        pnl: Optional[float] = None,
    ) -> None:
        # A flat close is refunded, not lost, so the caller's P/L wins over the
        # win/loss formula when it is supplied.
        if pnl is None:
            pnl = 0.0 if entry_price == exit_price else (stake * payout if won else -stake)
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO outcomes(signal_id, asset, direction, entry_price, "
                "exit_price, won, payout, pnl, stake, settled_at, reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    signal_id,
                    asset,
                    direction,
                    entry_price,
                    exit_price,
                    int(won),
                    payout,
                    pnl,
                    stake,
                    _iso(settled_at),
                    reason,
                ),
            )
            conn.execute("UPDATE signals SET status = 'settled' WHERE id = ?", (signal_id,))

    def joined_results(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """Signals joined to outcomes -- the table the analytics module reads.

        ``limit`` returns the most recent N trades (still oldest-first). Views
        that only display a page of history must pass one; loading the whole
        journal to show 500 rows is what made the app slow down over time.
        """
        sql = (
            "SELECT s.id, s.asset, s.direction, s.score, s.strategy, s.session, s.utc_hour, "
            "s.lead_seconds, s.features, s.entry_at, s.app_version, o.won, o.pnl, o.stake, o.payout, "
            "o.reason AS outcome_reason, o.entry_price, o.exit_price "
            "FROM signals s JOIN outcomes o ON o.signal_id = s.id "
        )
        if limit:
            sql += "ORDER BY s.entry_at DESC LIMIT ?"
            cur = self._conn.execute(sql, (limit,))
        else:
            sql += "ORDER BY s.entry_at"
            cur = self._conn.execute(sql)
        out = []
        for r in cur.fetchall():
            d = dict(r)
            d["features"] = json.loads(d.get("features") or "{}")
            out.append(d)
        if limit:
            out.reverse()   # DESC fetch, returned oldest-first like the unlimited call
        return out

    def stats(self) -> dict[str, float]:
        """Headline numbers computed in SQL.

        The GUI used to derive these by loading every settled trade into
        Python on each update, which grew linearly and stalled the interface
        as history accumulated. Aggregates belong in the database.
        """
        # Refunds (a flat close, stake returned) are settled trades but not
        # decided ones. Leaving them in the denominator understates the win
        # rate by counting a returned stake as a loss.
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(won), 0) AS wins, "
            "COALESCE(SUM(pnl), 0.0) AS pnl, "
            "COALESCE(SUM(entry_price = exit_price), 0) AS refunds "
            "FROM outcomes"
        ).fetchone()
        pending = self._conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status IN ('pending','active')"
        ).fetchone()[0]
        n = int(row["n"])
        refunds = int(row["refunds"])
        wins = int(row["wins"])
        decided = n - refunds
        return {
            "trades": n,
            "wins": wins,
            "losses": decided - wins,
            "refunds": refunds,
            "pnl": float(row["pnl"]),
            "win_rate": (wins / decided) if decided else 0.0,
            "pending": int(pending),
        }

    def count_signals_since(self, iso_prefix: str, include_cancelled: bool = False) -> int:
        """Signals detected on a given day, counted in SQL.

        Cancelled signals are excluded by default. A cancelled signal is one
        the engine withdrew before entry, so counting it as a signal makes the
        headline number disagree with what the user was actually asked to
        trade -- 566 on screen against a couple of dozen worth acting on.
        """
        sql = "SELECT COUNT(*) FROM signals WHERE detected_at >= ?"
        if not include_cancelled:
            sql += " AND status != 'cancelled'"
        return int(self._conn.execute(sql, (iso_prefix,)).fetchone()[0])

    def count_cancelled_since(self, iso_prefix: str) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM signals WHERE detected_at >= ? AND status = 'cancelled'",
                (iso_prefix,),
            ).fetchone()[0]
        )

    def abandon_stale_signals(self, before_iso: str) -> int:
        """Close out signals a previous session left hanging.

        A signal is written as 'pending' the moment it fires and only becomes
        'settled' or 'cancelled' when the engine reaches it. Closing the app
        in between leaves the row pending forever, and those orphans
        accumulate across every session -- which is why 'Awaiting entry' kept
        showing a number with nothing on screen.
        """
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE signals SET status = 'abandoned', "
                "cancel_reason = COALESCE(cancel_reason, 'the app closed before this signal resolved') "
                "WHERE status IN ('pending','active') AND detected_at < ?",
                (before_iso,),
            )
            return cur.rowcount or 0

    def summary_counts(self) -> dict[str, int]:
        c = self._conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        w = self._conn.execute("SELECT COUNT(*) FROM outcomes WHERE won = 1").fetchone()[0]
        r = self._conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE entry_price = exit_price"
        ).fetchone()[0]
        p = self._conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status IN ('pending','active')"
        ).fetchone()[0]
        return {"settled": int(c), "wins": int(w), "losses": int(c - w - r),
                "refunds": int(r), "pending": int(p)}
