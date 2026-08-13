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
        default=None,
        help="Path to signals.db (default: the app's Desktop/KPS folder).",
    )
    parser.add_argument(
        "--csv-dir",
        type=Path,
        default=None,
        help="Directory of CSV candle exports to include alongside the journal.",
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
        default=2000,
        help="Skip assets with fewer candles than this (default 2000).",
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

    histories = dataset.collect(
        db_path=args.db, csv_dir=args.csv_dir, min_bars=args.min_bars
    )
    sections.append(report.inventory(histories))

    if not histories:
        sections.append("")
        sections.append(
            "Nothing to analyse. Run the bot or the survey script to record"
        )
        sections.append(
            "candles first, or point --csv-dir at exported history."
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
