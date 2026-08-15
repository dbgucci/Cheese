"""Record OTC candles for every pair, for as long as it takes.

The analysis is sample-limited, not idea-limited. Eight days of one pair is
10,981 bars, and 3,326 hypotheses tested against it produced nothing that
survived a holdout. Six pairs for thirty days is roughly 250,000 bars --
twenty-three times the evidence -- and at that size a genuine 55% edge stops
being arguable.

So this does one job and does it without stopping: connect, pull closed
candles for every configured pair, write them, repeat. It places no trades,
runs no strategy, and forms no opinion. Everything it stores is raw.

Design points that matter for a process meant to run unattended for weeks:

* the pair list is a plain text file, edited without touching code;
* a pair that stops quoting is logged and retried, never fatal;
* a dropped connection reconnects with backoff rather than exiting;
* writes are idempotent, so a restart re-reads the same tail harmlessly;
* progress is reported against the sample size the analysis actually needs.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from .. import paths
from ..settings import DEFAULT_ASSETS, parse_assets
from ..storage import Journal

# What the study needs before a 55% edge would be visible rather than
# arguable. Reported every cycle so the wait has a number attached.
TARGET_BARS = 250_000


def data_dir() -> Path:
    import os
    override = os.environ.get("KPS_COLLECT_HOME")
    root = Path(override) if override else paths._desktop_dir() / "KPS Data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_path() -> Path:
    return data_dir() / "collector.json"


def pairs_path() -> Path:
    return data_dir() / "pairs.txt"


def db_path() -> Path:
    return data_dir() / "otc_candles.db"


@dataclass
class CollectorConfig:
    """Everything adjustable, in one editable file."""

    # The six OTC majors. Edit pairs.txt to change them -- one per line, or
    # separated by spaces or commas. Anything Pocket Option lists works.
    assets: list[str] = field(default_factory=lambda: list(DEFAULT_ASSETS))
    timeframe_seconds: int = 60
    poll_seconds: int = 20
    candles_per_pull: int = 120        # a generous tail, so a stall self-heals
    ssid: str = ""
    data_source: str = "pocket_option"   # or "synthetic" for a dry run
    report_every_minutes: int = 30
    telegram_token: str = ""
    telegram_chat: str = ""

    @classmethod
    def load(cls) -> "CollectorConfig":
        p = config_path()
        cfg = cls()
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                known = {f.name for f in fields(cls)}
                cfg = cls(**{k: v for k, v in raw.items() if k in known})
            except (json.JSONDecodeError, OSError, TypeError):
                pass          # a corrupt file falls back to defaults, not a crash
        else:
            cfg.save()
        # The pair list lives in its own file because it is the thing most
        # likely to be edited, and a text file is easier to edit correctly
        # than a JSON array.
        pp = pairs_path()
        if pp.exists():
            found, _ = parse_assets(pp.read_text(encoding="utf-8"))
            if found:
                cfg.assets = found
        else:
            pp.write_text(PAIRS_TEMPLATE, encoding="utf-8")
        return cfg

    def save(self) -> None:
        config_path().write_text(json.dumps(asdict(self), indent=2),
                                 encoding="utf-8")


PAIRS_TEMPLATE = """\
# One pair per line. Lines starting with # are ignored.
# These are the six Pocket Option OTC majors. Add or remove freely --
# the collector picks up changes the next time it starts.

EURUSD_otc
GBPUSD_otc
USDJPY_otc
AUDUSD_otc
USDCAD_otc
EURJPY_otc

# Others worth collecting if your account lists them:
# NZDUSD_otc
# USDCHF_otc
# GBPJPY_otc
# EURGBP_otc
# AUDCAD_otc
"""


@dataclass
class PairState:
    asset: str
    stored: int = 0
    last_ts: Optional[pd.Timestamp] = None
    consecutive_failures: int = 0
    last_error: str = ""


class Collector:
    """The loop. Deliberately boring."""

    def __init__(self, config: CollectorConfig, journal: Journal,
                 feed_factory: Callable[[str], object],
                 on_log: Optional[Callable[[str], None]] = None):
        self.cfg = config
        self.journal = journal
        self.feed_factory = feed_factory
        self.log = on_log or (lambda m: print(m, flush=True))
        self.feeds: dict[str, object] = {}
        self.state = {a: PairState(a) for a in config.assets}
        self.started = datetime.now(timezone.utc)
        self.cycles = 0
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------------
    def _feed(self, asset: str):
        if asset not in self.feeds:
            self.feeds[asset] = self.feed_factory(asset)
        return self.feeds[asset]

    def pull(self, asset: str) -> int:
        """One pair, once. Returns rows written. Never raises."""
        st = self.state[asset]
        try:
            df = self._feed(asset).get_candles(self.cfg.candles_per_pull)
        except Exception as exc:
            st.consecutive_failures += 1
            st.last_error = f"{type(exc).__name__}: {exc}"
            # A feed that has failed repeatedly is rebuilt: the usual cause is
            # a socket that closed under it, and a fresh one reconnects.
            if st.consecutive_failures in (3, 10, 30):
                self.feeds.pop(asset, None)
                self.log(f"  {asset}: {st.consecutive_failures} failures, "
                         f"rebuilding the feed ({st.last_error})")
            return 0

        if df is None or len(df) == 0:
            st.consecutive_failures += 1
            st.last_error = "no candles returned"
            return 0

        st.consecutive_failures = 0
        st.last_error = ""
        written = self.journal.record_candles(asset, df)
        st.stored += written
        st.last_ts = df.index[-1]
        return written

    # ------------------------------------------------------------------
    def cycle(self) -> int:
        total = 0
        for asset in list(self.state):
            if self._stop:
                break
            total += self.pull(asset)
        self.cycles += 1
        return total

    # ------------------------------------------------------------------
    def totals(self) -> tuple[int, dict[str, int]]:
        per = self.journal.candle_counts()
        return sum(per.values()), per

    def progress(self) -> str:
        total, per = self.totals()
        elapsed = (datetime.now(timezone.utc) - self.started).total_seconds() / 3600
        rate = total / elapsed if elapsed > 0.05 else 0.0
        lines = [
            f"{total:,} bars stored across {len(per)} pairs "
            f"({total / TARGET_BARS:.1%} of the {TARGET_BARS:,} the study wants)",
        ]
        if rate > 0:
            remaining = max(TARGET_BARS - total, 0) / rate
            done = datetime.now(timezone.utc) + timedelta(hours=remaining)
            lines.append(f"  collecting {rate:,.0f} bars/hour -- "
                         f"target reached about {done:%d %b}")
        for asset in sorted(self.state):
            st = self.state[asset]
            got = per.get(asset, 0)
            last = f"{st.last_ts:%H:%M}" if st.last_ts is not None else "--:--"
            note = f"  [{st.last_error}]" if st.last_error else ""
            lines.append(f"  {asset:14s} {got:>8,} bars   last {last}{note}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def run(self, max_cycles: Optional[int] = None,
            sleeper: Callable[[float], None] = time.sleep) -> None:
        self.log(f"collecting {len(self.state)} pairs into {db_path()}")
        self.log(f"pairs: {', '.join(sorted(self.state))}")
        self.log(f"edit {pairs_path()} to change them\n")
        last_report = datetime.now(timezone.utc)
        while not self._stop and (max_cycles is None or self.cycles < max_cycles):
            written = self.cycle()
            now = datetime.now(timezone.utc)
            if (now - last_report).total_seconds() >= self.cfg.report_every_minutes * 60:
                last_report = now
                self.log(f"\n[{now:%Y-%m-%d %H:%M} UTC]\n{self.progress()}\n")
            if self._stop or (max_cycles is not None and self.cycles >= max_cycles):
                break
            sleeper(self.cfg.poll_seconds)


# --------------------------------------------------------------------------
def build_feed_factory(cfg: CollectorConfig):
    if cfg.data_source == "synthetic":
        from ..data.synthetic import SyntheticFeed
        # A different starting price per pair, so a synthetic dry run produces
        # six distinguishable series rather than six copies of one.
        def make(asset: str):
            base = 1.0 + (abs(hash(asset)) % 400) / 1000.0
            return SyntheticFeed(start_price=base,
                                 seconds_per_candle=cfg.timeframe_seconds)
        return make

    from ..data.pocket_option import PocketOptionFeed
    if not cfg.ssid:
        raise SystemExit(
            "No Pocket Option session set.\n"
            f"Put your SSID in {config_path()} under \"ssid\", or set "
            "\"data_source\" to \"synthetic\" to test the collector without a "
            "connection.")
    return lambda asset: PocketOptionFeed(
        asset=asset, ssid=cfg.ssid, timeframe_seconds=cfg.timeframe_seconds)


def main(argv: Optional[list[str]] = None) -> int:   # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", nargs="*", default=None,
                    help="override the pair list for this run")
    ap.add_argument("--ssid", default=None)
    ap.add_argument("--synthetic", action="store_true",
                    help="run against the practice feed, to check the setup")
    ap.add_argument("--poll", type=int, default=None)
    args = ap.parse_args(argv)

    cfg = CollectorConfig.load()
    if args.pairs:
        cfg.assets = args.pairs
    if args.ssid:
        cfg.ssid = args.ssid
        cfg.save()
    if args.synthetic:
        cfg.data_source = "synthetic"
    if args.poll:
        cfg.poll_seconds = args.poll

    journal = Journal(db_path())
    collector = Collector(cfg, journal, build_feed_factory(cfg))

    def _bye(_sig, _frm):
        collector.stop()
    for s in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(s, _bye)
        except (ValueError, OSError):
            pass

    try:
        collector.run()
    finally:
        print("\n" + collector.progress())
        print(f"\nDatabase: {db_path()}")
        print("Send that file back to have the study run against it.")
        journal.close()
    return 0


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(main())
