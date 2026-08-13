"""Run the whole study.

    python -m cheese_signals.research.study data/*.db data/*.csv --payout 0.92

Search runs on the training slice only. Every candidate that survives the
multiplicity correction is then replayed on a holdout the search never
touched, and the report states how many hypotheses were tried so the reader
can judge the shortlist rather than being handed a conclusion.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import probes as P
from . import dataset, validate
from .probes import Finding


def search(panel: dict[str, pd.DataFrame], max_lag: int = 60) -> list[Finding]:
    """Every probe against every asset. This is the hypothesis count."""
    out: list[Finding] = []
    for asset, df in sorted(panel.items()):
        for seg_probe in (P.variance_ratio, P.quantisation):
            out += seg_probe(df, asset)
        out += P.autocorrelation(df, asset, max_lag=max_lag)
        out += P.streaks(df, asset)
        out += P.extremes(df, asset)
        out += P.repeats(df, asset)
        out += P.time_of_day(df, asset, by="hour")
        out += P.time_of_day(df, asset, by="minute")
    out += P.cross_pair(panel)
    return out


# --------------------------------------------------------------------------
# replaying a candidate on untouched data
# --------------------------------------------------------------------------
def _rule_for(f: Finding):
    """Turn a finding back into the rule that produced it.

    Deliberately explicit rather than clever: a candidate that cannot be
    stated as a rule cannot be traded, and one that cannot be replayed
    exactly cannot be validated.
    """
    probe, detail = f.probe, f.detail

    if probe == "autocorr":
        parts = detail.split()
        lag, side = int(parts[1]), parts[2]

        def rule(df):
            r = df["close"].diff()
            prev = r.shift(lag - 1)
            nxt = df["close"].shift(-1) - df["close"]
            m = (prev > 0) & (nxt != 0)
            up = nxt[m] > 0
            return int(up.sum() if side == "follow" else (~up).sum()), int(m.sum())
        return rule

    if probe == "streak":
        head, verb = detail.split(" -> ")
        k = int(head.split("x")[0])
        want = 1 if "up" in head else -1
        call = want if verb == "continues" else -want

        def rule(df):
            r = df["close"].diff()
            sign = np.sign(r).replace(0, np.nan).ffill()
            run = sign.groupby((sign != sign.shift()).cumsum()).cumcount() + 1
            nxt = df["close"].shift(-1) - df["close"]
            m = (run >= k) & (sign == want) & (nxt != 0)
            up = nxt[m] > 0
            return int(up.sum() if call > 0 else (~up).sum()), int(m.sum())
        return rule

    if probe == "extreme":
        bits = detail.split()
        want = 1 if bits[1] == "up" else -1
        q = int(bits[2][1:]) / 100.0
        call = -want if bits[-1] == "revert" else want

        def rule(df):
            r = df["close"].diff()
            thr = r.abs().quantile(q)
            nxt = df["close"].shift(-1) - df["close"]
            m = (r.abs() >= thr) & (np.sign(r) == want) & (nxt != 0)
            up = nxt[m] > 0
            return int(up.sum() if call > 0 else (~up).sum()), int(m.sum())
        return rule

    if probe == "time_of_day":
        by, value, side = detail.split()[0], int(detail.split()[1]), detail.split()[2]

        def rule(df):
            nxt = df["close"].shift(-1) - df["close"]
            key = df.index.hour if by == "hour" else df.index.minute
            m = (key == value) & (nxt != 0)
            up = nxt[m] > 0
            return int(up.sum() if side == "up" else (~up).sum()), int(m.sum())
        return rule

    return None


def replay(f: Finding, test_panel: dict[str, pd.DataFrame]) -> Optional[tuple[int, int]]:
    rule = _rule_for(f)
    if rule is None:
        return None
    if f.subject in test_panel:
        df = test_panel[f.subject]
        return rule(df) if len(df) > 50 else None
    return None


# --------------------------------------------------------------------------
def run(paths: list[str], payout: float = 0.92, test_fraction: float = 0.35,
        max_lag: int = 60) -> str:
    candles, sources = dataset.load(paths)
    lines = [dataset.summarise(candles, sources), ""]
    if candles.empty:
        return "\n".join(lines + ["Nothing to analyse."])

    panel = dataset.to_panel(candles)
    try:
        split = validate.split_by_time(panel, test_fraction)
    except ValueError as exc:
        return "\n".join(lines + [f"Cannot split: {exc}"])
    lines.append(f"SPLIT  {split.describe()}\n")

    found = search(split.train, max_lag=max_lag)
    lines.append(f"SEARCH  {len(found):,} hypotheses tested on the training slice\n")

    # Structural probes describe the series rather than proposing a trade;
    # they are reported whatever their p-value because they set the context
    # everything else is read in.
    structural = [f for f in found if f.probe in ("variance_ratio", "quantisation",
                                                  "repeat", "cross_pair")]
    lines.append("STRUCTURE")
    for f in sorted(structural, key=lambda x: x.p_value)[:40]:
        extra = f" {f.note}" if f.note else ""
        lines.append(f"  {f.probe:14s} {f.subject:14s} {f.detail:22s} "
                     f"effect={f.effect:<10.4f} p={f.p_value:.2e}{extra}")

    tradeable = [f for f in found
                 if f.probe in ("autocorr", "streak", "extreme", "time_of_day")
                 and np.isfinite(f.win_rate)]
    survivors = validate.benjamini_hochberg(tradeable)
    lines.append(f"\nCANDIDATES  {len(survivors)} of {len(tradeable)} "
                 f"tradeable hypotheses passed multiplicity correction")
    for f in sorted(survivors, key=lambda x: -x.win_rate)[:25]:
        lines.append("  " + str(f))

    verdicts = [validate.confirm(f, replay, split.test, payout)
                for f in sorted(survivors, key=lambda x: -x.win_rate)[:40]]
    lines.append("")
    lines.append(validate.report(verdicts, payout, tried=len(found)))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="signals.db and/or candle CSV files")
    ap.add_argument("--payout", type=float, default=0.92)
    ap.add_argument("--test-fraction", type=float, default=0.35, dest="test_fraction")
    ap.add_argument("--max-lag", type=int, default=60, dest="max_lag")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    text = run(args.paths, args.payout, args.test_fraction, args.max_lag)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
