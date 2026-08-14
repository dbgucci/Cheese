#!/usr/bin/env python3
"""Analyse every candle this project has ever recorded.

Run this on the machine that ran the bots -- the candle journal lives in
``<Desktop>/KPS/signals.db`` and is deliberately never committed to git, so
it does not travel with a clone of the repository.

    python run_research.py                      # journal only
    python run_research.py --csv-dir ./history  # journal + CSV exports
    python run_research.py --payout 0.80        # your actual payout
    python run_research.py --out report.txt     # save instead of printing

The report is written to be readable by someone who did not run it, and to
state a null result plainly when there is one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from cheese_signals.research import dataset, mine, randomwalk, report  # noqa: E402
from cheese_signals.research.stats import Payout  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mine recorded OTC candles for a statistically real edge.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=Path,
        nargs="*",
        default=None,
        help=(
            "One or more SQLite files holding candles. Default: the app's "
            "Desktop/KPS journal. Other bots' databases can be listed too -- "
            "any table with open/high/low/close columns is read."
        ),
    )
    parser.add_argument(
        "--csv-dir",
        type=Path,
        nargs="*",
        default=None,
        help=(
            "One or more directories of CSV candle exports, searched "
            "recursively. Non-candle CSVs are listed as skipped, not "
            "silently ignored."
        ),
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "Search your Desktop folders for every database and CSV that "
            "holds candles, and analyse all of them. Use this when the "
            "history is spread across several bots' data folders."
        ),
    )
    parser.add_argument(
        "--payout",
        type=float,
        default=0.92,
        help="Broker payout on a win, as a fraction (default 0.92 = 92%%).",
    )
    parser.add_argument(
        "--min-bars",
        type=int,
        default=1000,
        help=(
            "Skip assets with fewer candles than this (default 1000). The "
            "randomness tests are informative well below the threshold that "
            "pattern mining needs, so a small pair still earns a verdict."
        ),
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=mine.MIN_SAMPLES,
        help="Minimum decided trades before a rule is even considered.",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=0.05,
        help="False discovery rate for the Benjamini-Hochberg correction.",
    )
    parser.add_argument(
        "--no-pairs",
        action="store_true",
        help="Skip two-feature conjunctions (faster, tests far fewer rules).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the report to this file instead of stdout.",
    )
    return parser.parse_args(argv)


def build_report(args: argparse.Namespace) -> str:
    payout = Payout(rate=args.payout)
    sections: list[str] = [report.header(payout)]

    db_paths, csv_dirs = args.db, args.csv_dir
    if args.auto:
        found_dbs, found_csv_dirs = dataset.discover()
        db_paths = list(db_paths or []) + found_dbs
        csv_dirs = list(csv_dirs or []) + found_csv_dirs
        sections.append(
            f"Auto-discovery: {len(found_dbs)} database(s) and "
            f"{len(found_csv_dirs)} folder(s) containing CSVs found under your "
            "Desktop.\nEverything found is listed in the inventory below, "
            "including what could not be used."
        )
        if not db_paths:
            db_paths = [None]

    collection = dataset.collect(
        db_path=db_paths, csv_dir=csv_dirs, min_bars=args.min_bars
    )
    sections.append(report.inventory(collection))
    histories = collection.histories

    if not histories:
        sections.append(
            "Nothing to analyse. Run the bot to record candles first, or point"
            "\n--csv-dir at exported history. The skip reasons above say why"
            "\neach source that was checked could not be used."
        )
        return "\n\n".join(sections)

    # Step 1 runs on every asset before any mining, so the randomness verdict
    # is visible before the tables it should be read against.
    predictability = [
        randomwalk.analyze(h.candles["close"], asset=h.asset) for h in histories
    ]
    sections.append(report.predictability(predictability))

    for history in histories:
        result = mine.mine_asset(
            history.candles,
            asset=history.asset,
            payout=payout,
            alpha=args.alpha,
            min_samples=args.min_samples,
            pair_features=not args.no_pairs,
        )
        sections.append(report.mining(result))

    sections.append(report.footer())
    return "\n\n".join(sections)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    text = build_report(args)

    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"Report written to {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
