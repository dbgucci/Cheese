#!/usr/bin/env python3
"""Record Pocket Option OTC candles continuously, so the research has data.

The first real research run found the archive held 3.8 days of candles.
Testing an hour-of-day rule needs 5.6 days before one hypothesis becomes
measurable, and the conditional patterns worth looking for need about four
weeks. That gap is the whole problem, and only running time closes it.

    python run_recorder.py                      # default watchlist
    python run_recorder.py --assets EURUSD_otc GBPUSD_otc
    python run_recorder.py --status             # how much history exists

Leave it running. It places no orders, needs no strategy, and reconnects
by itself. Check back in a fortnight and run the research again.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from cheese_signals import storage  # noqa: E402
from cheese_signals.research import recorder as rec  # noqa: E402

# The six pairs already in the journal, so a default run deepens the history
# that exists rather than starting six shallow new ones.
DEFAULT_ASSETS = [
    "EURUSD_otc",
    "GBPUSD_otc",
    "AUDUSD_otc",
    "USDJPY_otc",
    "USDCAD_otc",
    "EURJPY_otc",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record OTC candles to the journal, continuously.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--assets",
        nargs="+",
        default=DEFAULT_ASSETS,
        help="Assets to record (default: the six already in the journal).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=30.0,
        help="Seconds between polls (default 30; candles close every 60).",
    )
    parser.add_argument(
        "--timeframe-seconds",
        type=int,
        default=60,
        help="Candle size in seconds (default 60).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print how much history exists and what it can test, then exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.status:
        print(rec.progress_report())
        return 0

    from cheese_signals.data.pocket_option import get_pocket_option_feed

    def factory(asset: str):
        return get_pocket_option_feed(
            asset, timeframe_seconds=args.timeframe_seconds
        )

    journal = storage.Journal()
    recorder = rec.Recorder(
        assets=args.assets,
        feed_factory=factory,
        journal=journal,
        poll_seconds=args.poll_seconds,
    )
    rec.install_signal_handlers(recorder)

    print(f"Recording {len(args.assets)} asset(s) to {journal.path}")
    print("Ctrl+C to stop. Leave this running -- more days is the whole point.\n")

    try:
        stats = recorder.run()
    except KeyboardInterrupt:
        stats = recorder.stats

    print()
    print(stats.summary())
    print()
    print(rec.progress_report(journal))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
