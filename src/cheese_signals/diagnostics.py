"""A live trace of what the engine is reading and why it did or didn't fire.

Without this, "no signals for 30 minutes" is indistinguishable from "the app
is broken". Several conditions can suppress every signal indefinitely with no
visible symptom at all -- not enough candles to warm up an EMA 200 being the
worst, because it never resolves itself if the feed only ever returns a short
history.

Traces are kept in memory (a bounded ring, so this cannot become the next
thing that slows the app down) and mirrored to a daily file in the data
folder so a session can be reviewed or shared afterwards.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from . import paths

MAX_TRACES = 600


@dataclass
class Trace:
    """One evaluation of one asset at one candle."""

    at: datetime
    asset: str
    outcome: str                     # FIRED | SUPPRESSED | NO SETUP | ERROR
    summary: str
    checks: list[str] = field(default_factory=list)
    candles: int = 0
    score: float = 0.0
    direction: int = 0

    @property
    def fired(self) -> bool:
        return self.outcome == "FIRED"

    @property
    def is_problem(self) -> bool:
        """Something the user needs to see regardless of the current filter.

        Filtering to "signals only" must not be able to hide the reason there
        are no signals -- that is the exact situation the filter gets used in.
        """
        return self.outcome in ("ERROR", "CONFIG WARNING")

    def as_text(self) -> str:
        head = f"{self.at:%H:%M:%S}  {self.asset:<12} {self.outcome:<10} {self.summary}"
        if not self.checks:
            return head
        return head + "\n" + "\n".join(f"                          {c}" for c in self.checks)


class TraceLog:
    """Thread-safe bounded log. The engine writes; the GUI reads."""

    def __init__(self, maxlen: int = MAX_TRACES, write_file: bool = True):
        self._items: deque[Trace] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._write_file = write_file
        self._day: Optional[str] = None
        self._fh = None

    def add(self, trace: Trace) -> None:
        with self._lock:
            self._items.append(trace)
            if self._write_file:
                self._write(trace)

    def _write(self, trace: Trace) -> None:
        try:
            day = trace.at.strftime("%Y-%m-%d")
            if day != self._day:
                if self._fh:
                    self._fh.close()
                self._fh = open(paths.logs_dir() / f"engine-{day}.log", "a", encoding="utf-8")
                self._day = day
            self._fh.write(trace.as_text() + "\n")
            self._fh.flush()
        except OSError:
            # Logging must never take the engine down.
            self._write_file = False

    def recent(self, limit: int = 200, asset: Optional[str] = None,
               only_fired: bool = False) -> list[Trace]:
        with self._lock:
            items = list(self._items)
        if asset:
            items = [t for t in items if t.asset == asset]
        if only_fired:
            items = [t for t in items if t.fired or t.is_problem]
        return items[-limit:]

    def counts(self) -> dict[str, int]:
        with self._lock:
            items = list(self._items)
        out: dict[str, int] = {}
        for t in items:
            out[t.outcome] = out.get(t.outcome, 0) + 1
        return out

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


def make_trace(
    asset: str,
    outcome: str,
    summary: str,
    checks: Iterable[str] = (),
    candles: int = 0,
    score: float = 0.0,
    direction: int = 0,
    at: Optional[datetime] = None,
) -> Trace:
    return Trace(
        at=at or datetime.now(timezone.utc),
        asset=asset,
        outcome=outcome,
        summary=summary,
        checks=list(checks),
        candles=candles,
        score=score,
        direction=direction,
    )
