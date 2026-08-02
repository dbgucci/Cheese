"""Reflection layer: turn the outcome journal into tuning decisions.

Everything here reads only from your own logged results, so as the journal
fills up the numbers stop being priors and start being facts about your
broker's actual feed.

The key output is ``breakdown()``: realised win rate sliced by the dimensions
that the engine can actually act on -- strategy, session/hour, asset, lead
time, and confidence bucket -- each compared against the break-even win rate
implied by your payout. A slice below break-even with a meaningful sample is
a candidate to disable; a slice well above it is a candidate to favour.

``suggestions()`` turns those slices into concrete, conservative advice, and
deliberately refuses to say anything at all about slices with small samples.
Twelve trades is not evidence, and the most common way a bot like this loses
money is by "learning" from noise.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

MIN_SAMPLE = 30  # below this, report but never advise


def breakeven_win_rate(payout: float) -> float:
    return 1.0 / (1.0 + payout)


@dataclass
class Slice:
    key: str
    trades: int
    wins: int
    pnl: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.pnl / self.trades if self.trades else 0.0

    def edge_vs_breakeven(self, payout: float) -> float:
        return self.win_rate - breakeven_win_rate(payout)

    @property
    def is_significant(self) -> bool:
        return self.trades >= MIN_SAMPLE


def _group(rows: Iterable[dict[str, Any]], keyfn: Callable[[dict[str, Any]], Optional[str]]) -> list[Slice]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        k = keyfn(r)
        if k is not None:
            buckets[k].append(r)
    out = [
        Slice(
            key=k,
            trades=len(v),
            wins=sum(1 for x in v if x.get("won")),
            pnl=sum(float(x.get("pnl") or 0.0) for x in v),
        )
        for k, v in buckets.items()
    ]
    return sorted(out, key=lambda s: (-s.trades, s.key))


def _lead_bucket(r: dict[str, Any]) -> str:
    secs = int(r.get("lead_seconds") or 0)
    if secs <= 0:
        return "immediate"
    mins = secs // 60
    return f"{mins}min lead" if mins <= 5 else "5min+ lead"


def _score_bucket(r: dict[str, Any]) -> str:
    s = float(r.get("score") or 0.0)
    lo = int(s * 10) / 10
    return f"{lo:.1f}-{lo + 0.1:.1f}"


def breakdown(rows: list[dict[str, Any]]) -> dict[str, list[Slice]]:
    return {
        "strategy": _group(rows, lambda r: r.get("strategy") or "unknown"),
        "session": _group(rows, lambda r: r.get("session") or "unknown"),
        "hour": _group(rows, lambda r: f"{int(r.get('utc_hour', -1)):02d}:00 UTC"),
        "asset": _group(rows, lambda r: r.get("asset")),
        "lead": _group(rows, _lead_bucket),
        "confidence": _group(rows, _score_bucket),
    }


def overall(rows: list[dict[str, Any]]) -> Slice:
    return Slice(
        key="overall",
        trades=len(rows),
        wins=sum(1 for r in rows if r.get("won")),
        pnl=sum(float(r.get("pnl") or 0.0) for r in rows),
    )


def loss_reasons(rows: list[dict[str, Any]], limit: int = 10) -> list[tuple[str, int]]:
    """Most frequent conditions appearing in losing trades' attributed reasons."""
    tally: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get("won"):
            continue
        reason = r.get("outcome_reason") or ""
        for part in reason.split(";"):
            part = part.strip().rstrip(".")
            # Skip the per-trade specifics; keep the reusable conditions.
            if not part or part.startswith("Loss:") or "moved" in part or "confidence was" in part:
                continue
            tally[part] += 1
    return sorted(tally.items(), key=lambda kv: -kv[1])[:limit]


def drift_check(rows: list[dict[str, Any]], payout: float = 0.85) -> list[str]:
    """Compare the strategy against always-BUY / always-SELL on the same windows.

    Without this, a directional market makes a useless signal look skilful. If
    price rose in 65% of the minutes you traded, every BUY looks smart and
    every SELL looks broken -- and "disable SELL" is then exactly the wrong
    conclusion, because the next session's drift can run the other way.

    A strategy only demonstrates skill by beating the base rate of the windows
    it chose, not by beating 50%.
    """
    usable = [
        r for r in rows
        if r.get("entry_price") is not None
        and r.get("exit_price") is not None
        and r["entry_price"] != r["exit_price"]
    ]
    if len(usable) < MIN_SAMPLE:
        return []

    n = len(usable)
    rose = sum(1 for r in usable if r["exit_price"] > r["entry_price"])
    up_rate = rose / n
    actual = sum(1 for r in usable if r.get("won")) / n

    def net(wins: int) -> float:
        return wins * payout - (n - wins)

    out = [
        f"Price rose in {up_rate:.0%} of the {n} minutes you traded "
        f"(a neutral market would be ~50%)."
    ]
    best_side = "BUY" if up_rate >= 0.5 else "SELL"
    best_wins = rose if up_rate >= 0.5 else n - rose
    out.append(
        f"Benchmark: always {best_side} on these same windows would have won "
        f"{best_wins / n:.1%} ({net(best_wins):+.1f} units). "
        f"Your signals won {actual:.1%} ({net(int(actual * n)):+.1f} units)."
    )
    if actual < best_wins / n:
        out.append(
            "Your signals did NOT beat simply always taking the drifting side, so this "
            "sample shows no directional skill -- and copying the drift is not a strategy, "
            "because it reverses without warning."
        )
    if abs(up_rate - 0.5) > 0.08:
        out.append(
            "Because the sample is directionally skewed, treat per-session and per-pair "
            "win rates with suspicion: they mostly reflect which way price happened to "
            "move, not which conditions work."
        )
    return out


def suggestions(rows: list[dict[str, Any]], payout: float = 0.85) -> list[str]:
    """Conservative, sample-size-aware tuning advice. Silent when data is thin."""
    if len(rows) < MIN_SAMPLE:
        return [
            f"Only {len(rows)} settled trades logged. Collect at least {MIN_SAMPLE} "
            "before drawing any conclusions -- anything sooner is noise."
        ]

    be = breakeven_win_rate(payout)
    out: list[str] = []
    ov = overall(rows)
    out.append(
        f"Overall: {ov.trades} trades, {ov.win_rate:.1%} win rate vs {be:.1%} needed to break even "
        f"({ov.pnl:+.2f} net)."
    )

    # Drift first: it determines whether the per-slice numbers below mean
    # anything at all.
    out.extend(drift_check(rows, payout=payout))

    bd = breakdown(rows)

    for dim, label in [
        ("strategy", "strategy"),
        ("session", "session"),
        ("asset", "asset"),
        ("lead", "lead time"),
        ("confidence", "confidence bucket"),
    ]:
        sig = [s for s in bd[dim] if s.is_significant]
        if not sig:
            continue
        worst = min(sig, key=lambda s: s.win_rate)
        best = max(sig, key=lambda s: s.win_rate)
        if worst.win_rate < be - 0.03:
            out.append(
                f"Weak {label}: '{worst.key}' is {worst.win_rate:.1%} over {worst.trades} trades "
                f"({worst.edge_vs_breakeven(payout):+.1%} vs break-even). Consider disabling it."
            )
        if best.win_rate > be + 0.03 and best.key != worst.key:
            out.append(
                f"Strong {label}: '{best.key}' is {best.win_rate:.1%} over {best.trades} trades "
                f"({best.edge_vs_breakeven(payout):+.1%} vs break-even). Consider favouring it."
            )

    lead_slices = [s for s in bd["lead"] if s.is_significant]
    if len(lead_slices) >= 2:
        by_lead = sorted(lead_slices, key=lambda s: s.key)
        spread = max(s.win_rate for s in by_lead) - min(s.win_rate for s in by_lead)
        if spread > 0.05:
            out.append(
                f"Advance warning is costing you: win rate varies {spread:.1%} across lead times. "
                "Lower 'lead_minutes' in Settings if longer leads are the weaker bucket."
            )

    if not any(s.startswith(("Weak", "Strong")) for s in out):
        out.append("No single dimension stands out yet. Keep collecting data.")

    return out
