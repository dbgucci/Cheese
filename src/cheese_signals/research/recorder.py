"""Record candles and nothing else, for as long as you leave it running.

The research run made the binding constraint obvious: the archive held 3.8
days of candles, and testing an hour-of-day rule needs 5.6 days before a
single hypothesis becomes measurable. No amount of cleverness substitutes
for that; only calendar time does.

``watch`` mode cannot fill the gap, because it is a trading loop that
happens to store what it sees. It skips low-liquidity sessions entirely,
which on a 24/7 OTC feed means it throws away exactly the hours nobody has
data for -- and those are the hours worth checking. It also follows one
asset at a time.

So this records instead of trading:

* every asset in the watchlist, from one shared connection
* every candle, including weekends and dead hours -- the point is coverage
* reconnecting on failure rather than exiting, because a fortnight-long
  run will meet a dropped socket and a process that dies at 3am has
  quietly cost you a week
* writing straight to the journal the research tools already read

It places no orders and needs no strategy. Start it and leave it alone.
"""

from __future__ import annotations

import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from .. import storage

# Backing off after a failure keeps a broker outage from becoming a tight
# reconnect loop, but the cap stays low: every minute spent backed off is a
# candle that no longer exists anywhere.
INITIAL_BACKOFF = 5.0
MAX_BACKOFF = 300.0


@dataclass
class AssetStats:
    written: int = 0
    errors: int = 0
    last_write: Optional[datetime] = None


@dataclass
class RecorderStats:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    per_asset: dict[str, AssetStats] = field(default_factory=dict)
    polls: int = 0
    reconnects: int = 0

    def stat(self, asset: str) -> AssetStats:
        return self.per_asset.setdefault(asset, AssetStats())

    @property
    def total_written(self) -> int:
        return sum(s.written for s in self.per_asset.values())

    @property
    def elapsed_hours(self) -> float:
        delta = datetime.now(timezone.utc) - self.started_at
        return delta.total_seconds() / 3600.0

    def summary(self) -> str:
        hours = self.elapsed_hours
        rate = self.total_written / hours if hours > 0 else 0.0
        return (
            f"{self.total_written:,} candles over {hours:.1f}h "
            f"({rate:,.0f}/h), {self.polls:,} polls, {self.reconnects} reconnect(s)"
        )


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


class Recorder:
    """Polls a set of feeds and writes every new candle to the journal."""

    def __init__(
        self,
        assets: Sequence[str],
        feed_factory: Callable[[str], object],
        journal: Optional[storage.Journal] = None,
        poll_seconds: float = 30.0,
        history_bars: int = 120,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _default_now,
        stale_after_seconds: float = 600.0,
    ):
        if not assets:
            raise ValueError("recorder needs at least one asset")
        self.assets = list(assets)
        self.feed_factory = feed_factory
        self.journal = journal or storage.Journal()
        self.poll_seconds = poll_seconds
        self.history_bars = history_bars
        self._sleep = sleep
        self._now = now
        self.stale_after_seconds = stale_after_seconds
        self._last_any_write: Optional[datetime] = None
        self._last_stale_warning: Optional[datetime] = None

        self.stats = RecorderStats(started_at=now())
        self._feeds: dict[str, object] = {}
        self._failures: dict[str, int] = {}
        self._wait: dict[str, float] = {}
        self._stop = False

    def _backoff_for(self, failures: int) -> float:
        """Seconds to wait after ``failures`` consecutive errors.

        Doubling from INITIAL_BACKOFF, capped. Derived from the failure count
        rather than the previous wait so it cannot drift, and so a single
        success resets it exactly.
        """
        if failures <= 0:
            return 0.0
        return min(INITIAL_BACKOFF * (2 ** (failures - 1)), MAX_BACKOFF)

    def stop(self) -> None:
        self._stop = True

    def _feed_for(self, asset: str):
        feed = self._feeds.get(asset)
        if feed is None:
            feed = self.feed_factory(asset)
            self._feeds[asset] = feed
        return feed

    def _drop_feed(self, asset: str) -> None:
        """Forget a feed so the next poll rebuilds it.

        A socket that has failed once usually keeps failing; recreating it is
        what actually recovers, and is why the process does not need to be
        restarted by hand after an outage.
        """
        if self._feeds.pop(asset, None) is not None:
            self.stats.reconnects += 1

    def poll_once(self) -> int:
        """One pass over every asset. Returns candles written this pass."""
        written = 0
        for asset in self.assets:
            if self._stop:
                break

            # An asset that just failed waits out its own backoff without
            # holding up the others. The decrement is never zero, so a fast
            # poll interval cannot leave an asset waiting forever.
            due = self._wait.get(asset, 0.0)
            if due > 0:
                self._wait[asset] = max(0.0, due - max(self.poll_seconds, 1.0))
                continue

            try:
                df = self._feed_for(asset).get_candles(self.history_bars)
                count = self.journal.record_candles(asset, df)
            except Exception as exc:  # noqa: BLE001 - a recorder must not die
                stat = self.stats.stat(asset)
                stat.errors += 1
                self._drop_feed(asset)
                failures = self._failures.get(asset, 0) + 1
                self._failures[asset] = failures
                self._wait[asset] = self._backoff_for(failures)
                print(
                    f"[{self._now():%H:%M:%S}] {asset}: {type(exc).__name__}: {exc} "
                    f"-- retrying in {self._wait[asset]:.0f}s",
                    file=sys.stderr,
                )
                continue

            self._failures[asset] = 0
            self._wait[asset] = 0.0
            if count:
                stat = self.stats.stat(asset)
                stat.written += count
                stat.last_write = self._now()
                self._last_any_write = stat.last_write
                written += count

        self.stats.polls += 1
        return written

    def stale_warning(self) -> Optional[str]:
        """Warn when nothing has been recorded for a long time.

        The likeliest cause on a multi-week run is an expired
        ``POCKET_OPTION_SSID``: session tokens do not last a month, and once
        one lapses the recorder keeps retrying and logging politely forever
        while writing nothing. Left unsaid, that turns "I recorded for three
        weeks" into an empty journal discovered three weeks later.
        """
        now = self._now()
        reference = self._last_any_write or self.stats.started_at
        idle = (now - reference).total_seconds()
        if idle < self.stale_after_seconds:
            return None

        # Repeat the warning periodically rather than on every poll.
        if self._last_stale_warning is not None:
            since = (now - self._last_stale_warning).total_seconds()
            if since < self.stale_after_seconds:
                return None

        self._last_stale_warning = now
        errors = sum(s.errors for s in self.stats.per_asset.values())
        return (
            f"WARNING: nothing recorded for {idle / 60:.0f} minutes "
            f"({errors} feed error(s) so far).\n"
            "  The usual cause is an expired POCKET_OPTION_SSID. Log in to "
            "pocketoption.com,\n"
            "  copy a fresh session id, set it, and restart the recorder."
        )

    def run(self, max_polls: Optional[int] = None, status_every: int = 20) -> RecorderStats:
        """Poll until stopped. ``max_polls`` bounds it, for tests."""
        polls = 0
        while not self._stop and (max_polls is None or polls < max_polls):
            written = self.poll_once()
            polls += 1

            if written or self.stats.polls % status_every == 0:
                print(
                    f"[{self._now():%Y-%m-%d %H:%M:%S}] +{written} candles | "
                    f"{self.stats.summary()}",
                    flush=True,
                )

            warning = self.stale_warning()
            if warning:
                print(warning, file=sys.stderr, flush=True)

            if not self._stop and (max_polls is None or polls < max_polls):
                self._sleep(self.poll_seconds)

        return self.stats


def install_signal_handlers(recorder: Recorder) -> None:
    """Stop cleanly on Ctrl+C, so the journal is committed rather than killed."""

    def handler(signum, frame):  # noqa: ARG001
        print("\nStopping -- finishing the current poll first.")
        recorder.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # not on the main thread, or unsupported on this platform


def progress_report(journal: Optional[storage.Journal] = None) -> str:
    """How much history exists, and what it is now enough to test.

    The thresholds come from the miner's own requirement: a rule needs 200
    observations inside a 60% training split before it can be scored at all.
    """
    journal = journal or storage.Journal()
    lines = ["Recorded history per asset:", ""]

    try:
        counts = journal.candle_counts_by_asset()
    except AttributeError:
        return "Journal does not expose per-asset counts."

    if not counts:
        return "No candles recorded yet."

    for asset, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        days = count / 1440.0
        lines.append(f"  {asset:<20}{count:>9,} bars  ({days:>5.1f} days)")

    smallest = min(counts.values())
    lines.append("")
    lines.append("What that sample can test (smallest asset):")
    for label, need in (
        ("whole-feed rules", 333),
        ("hour-of-day rules", 8_000),
        ("hour x direction", 16_000),
        ("hour x volatility bucket", 40_000),
    ):
        status = "yes" if smallest >= need else f"no -- needs {need:,}"
        lines.append(f"  {label:<26}{status}")

    return "\n".join(lines)
