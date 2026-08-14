"""Turn the research output into a report that cannot be misread as a promise.

Deliberate choices here, because the formatting of a result changes how it
gets used:

* The predictability verdict is printed **first**, per asset. If a feed shows
  no structure, every table below it is noise and the reader should know that
  before reading them.
* Win rates are always printed next to the break-even line and a confidence
  interval, never alone. "58%" is a sales pitch; "58% [51%, 65%] vs 52.08%
  break-even" is a measurement.
* The number of hypotheses tested is stated on every mining table, because a
  finding's credibility depends entirely on how many places you looked.
* A null result gets stated in one clear sentence rather than buried.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .dataset import AssetHistory
from .mine import Candidate, MiningReport, sample_size_note
from .randomwalk import PredictabilityReport
from .stats import Payout

RULE = "=" * 78
THIN = "-" * 78

# A Desktop-wide auto-scan rejects a lot of unrelated CSVs; show enough to
# spot a mistake without burying the report.
MAX_SKIPS_SHOWN = 25


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def inventory(collection) -> str:
    """What data actually exists, before any analysis of it.

    Accepts a ``dataset.Collection`` (or a plain sequence of histories).
    """
    histories = getattr(collection, "histories", None)
    if histories is None:
        histories = list(collection)
    skipped = list(getattr(collection, "skipped", []))
    sources = list(getattr(collection, "sources_read", []))

    lines = [RULE, "DATA INVENTORY", RULE]

    if not histories:
        lines += [
            "",
            "No candle history found.",
            "",
            "Nothing to analyse. Every source that was checked, and why it",
            "could not be used, is listed below.",
        ]
    else:
        total = sum(h.n_bars for h in histories)
        lines.append(f"{len(histories)} asset(s), {total:,} candles total")
        lines.append("")
        lines.append(f"{'asset':<26}{'bars':>10}{'coverage':>11}  span")
        lines.append(THIN)
        for h in histories:
            lines.append(
                f"{h.asset:<26}{h.n_bars:>10,}{h.coverage:>10.0%}  {h.span}"
            )

        lines.append("")
        lines.append(
            "Coverage is recorded bars / calendar minutes in the span. Gaps are"
        )
        lines.append(
            "periods no bot was running; they are split apart before mining so no"
        )
        lines.append("rule is ever scored across one.")

    if sources:
        lines.append("")
        lines.append(f"Sources read ({len(sources)}):")
        for source in sources:
            lines.append(f"  + {source}")

    if skipped:
        # A Desktop-wide scan can reject hundreds of unrelated CSVs. Show the
        # ones most likely to be real data first -- anything rejected for a
        # reason other than "obviously not candles" -- then cap the rest.
        def _uninteresting(note) -> bool:
            return "no timestamp column" in note.reason or "not candle data" in note.reason

        notable = [n for n in skipped if not _uninteresting(n)]
        ordinary = [n for n in skipped if _uninteresting(n)]
        shown = (notable + ordinary)[:MAX_SKIPS_SHOWN]

        lines.append("")
        lines.append(f"Sources NOT used ({len(skipped)}):")
        for note in shown:
            lines.append(f"  - {note.path}")
            lines.append(f"      {note.reason}")
        if len(skipped) > len(shown):
            lines.append(
                f"  ... and {len(skipped) - len(shown):,} more, all rejected for"
            )
            lines.append("      having no timestamp or no OHLC columns.")
        lines.append("")
        lines.append(
            "Check this list. A file skipped for the wrong reason is data you"
        )
        lines.append("think was analysed and was not.")

    return "\n".join(lines)


def predictability(reports: Iterable[PredictabilityReport]) -> str:
    """The section that governs how everything after it should be read."""
    lines = [RULE, "STEP 1 - IS THE FEED PREDICTABLE AT ALL?", RULE]
    lines.append("")
    lines.append(
        "If a feed is a random walk, no algorithm can beat 50%, and at a 92%"
    )
    lines.append(
        "payout every trade has an expectancy of -4.2%. This runs first."
    )
    lines.append("")

    for report in reports:
        lines.append(f"{report.asset}  ({report.n_bars:,} bars)")
        lines.append(THIN)
        for test in report.tests:
            flag = "**" if test.significant else "  "
            lines.append(
                f" {flag} {test.name:<24} p={test.pvalue:.4f}  {test.detail}"
            )
        lines.append("")
        lines.append(f"  VERDICT: {report.verdict}")
        lines.append("")

    lines.append("** marks p < 0.05 before multiple-testing correction.")
    lines.append(
        "With ~14 tests per asset, expect ~0.7 false flags per asset by chance"
    )
    lines.append("alone -- a single ** is not evidence of anything.")
    return "\n".join(lines)


def _candidate_row(candidate: Candidate, payout: Payout) -> str:
    lo, hi = candidate.test.interval()
    return (
        f"  {candidate.name:<40}"
        f"{candidate.train.win_rate:>8.2%}"
        f"{candidate.validate.win_rate:>9.2%}"
        f"{candidate.test.win_rate:>8.2%}"
        f"  [{lo:.0%},{hi:.0%}]"
        f"{candidate.test.decided:>7,}"
        f"{candidate.test.expectancy(payout):>9.2%}"
    )


def mining(report: MiningReport) -> str:
    """One asset's mining results."""
    payout = report.payout
    lines = [RULE, f"STEP 2 - PATTERN SEARCH: {report.asset}", RULE]
    lines.append("")
    lines.append(
        f"Payout {payout.rate:.0%}  ->  break-even win rate "
        f"{payout.breakeven:.2%}"
    )

    if report.insufficient_data:
        lines.append("")
        lines.append(f"  RESULT: {report.verdict}")
        lines.append("")
        lines.append(
            "  Nothing was searched here, so read this as a gap in the data,"
        )
        lines.append("  not as a finding about the feed.")
        return "\n".join(lines)

    tr, va, te = report.split_sizes
    lines.append(
        f"Split (chronological): train {tr:,} / validate {va:,} / test {te:,} bars"
    )
    lines.append(f"Hypotheses examined: {report.n_candidates_tested:,}")
    lines.append(
        f"Survived FDR correction on train: {report.n_passed_significance}"
    )
    lines.append("")

    if not report.findings:
        lines.append(f"  RESULT: {report.verdict}")
        lines.append("")
        lines.append(f"  {sample_size_note(payout)}")
        return "\n".join(lines)

    lines.append(
        f"  {'rule':<40}{'train':>8}{'valid':>9}{'test':>8}"
        f"  {'95% CI':<10}{'n':>6}{'exp/trade':>9}"
    )
    lines.append(THIN)
    for candidate in report.findings:
        lines.append(_candidate_row(candidate, payout))

    lines.append("")
    confirmed = report.confirmed
    if confirmed:
        lines.append(f"  CONFIRMED OUT OF SAMPLE: {len(confirmed)}")
        for candidate in confirmed:
            lines.append(
                f"    {candidate.name}  "
                f"(train->test decay {candidate.decay:+.2%}, q={candidate.qvalue:.4f})"
            )
    else:
        lines.append(
            "  CONFIRMED OUT OF SAMPLE: 0 -- every rule above decayed to"
        )
        lines.append(
            "  break-even or worse on unseen data. That is what overfitting"
        )
        lines.append("  looks like when you actually measure it.")

    lines.append("")
    lines.append(f"  RESULT: {report.verdict}")
    lines.append("")
    lines.append(f"  {sample_size_note(payout)}")
    return "\n".join(lines)


def header(payout: Payout) -> str:
    return "\n".join(
        [
            RULE,
            "OTC RESEARCH REPORT",
            RULE,
            "",
            "Method:",
            "  1. Test each feed for any departure from a random walk.",
            "  2. Mine bar patterns on a chronological training split only.",
            "  3. Correct for the number of patterns examined (Benjamini-Hochberg).",
            "  4. Re-score survivors on validation, then once on a held-out test",
            "     split. Report the decay between them.",
            "",
            "Every win rate below is measured against the payout break-even",
            f"({payout.breakeven:.2%} at a {payout.rate:.0%} payout), not against 50%.",
            "",
        ]
    )


def footer() -> str:
    return "\n".join(
        [
            RULE,
            "HOW TO READ THIS",
            RULE,
            "",
            "A rule is worth testing on a demo account only if it:",
            "  - came from a feed that failed at least one randomness test,",
            "  - survived the FDR correction on training data,",
            "  - stayed above break-even on BOTH validation and test splits,",
            "  - has a test-split confidence interval whose lower bound is",
            "    above break-even, and",
            "  - has enough decided trades to make that interval meaningful.",
            "",
            "A rule that fails any of those is a coincidence. Trading it is",
            "paying the broker for the privilege of confirming that.",
            "",
            "Forward-test anything that survives on a demo account for the",
            "sample size printed above before risking money. Historical",
            "confirmation is necessary, not sufficient: an OTC feed is",
            "generated by the broker and can change without notice.",
            "",
        ]
    )
