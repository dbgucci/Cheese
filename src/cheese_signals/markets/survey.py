"""Run the cost wall against a live broker account and report the verdict.

This is step zero of the real-market project, and it is deliberately the
whole of step zero. It answers one question per instrument -- *is there room
here for any strategy at all* -- using the broker's own quoted spreads, and
it answers it in an afternoon rather than after a thousand live trades.

    python -m cheese_signals.markets.survey --days 90

The output is a table and a set of plain sentences. An instrument whose
typical move over the intended holding period is smaller than twice the
round-trip cost should not be traded on that holding period, regardless of
how good the strategy looks, because the strategy is being asked to beat a
handicap that published edges do not beat.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from . import costs
from .mt5_bridge import M1, Broker, MT5Feed, fetch_all

# The instruments asked for, under the names brokers most commonly use. The
# survey resolves them against what the account actually offers rather than
# assuming, because CFD naming is not standardised and a missing suffix is
# the difference between a study and an empty table.
DEFAULT_SYMBOLS = [
    "XAUUSD", "US30", "SPX500", "NAS100",
    "EURUSD", "GBPUSD", "USDJPY", "EURGBP", "AUDUSD", "USDCAD",
]

# Holding periods to test, from scalping to the intraday horizons that carry
# published evidence. The point of spanning the range is to show where the
# wall sits rather than to argue about it.
DEFAULT_HORIZONS = (1, 5, 15, 30, 60, 240)

# Liquid-session windows in UTC. Outside these, spreads widen and the move
# does not, which is the worst possible combination.
SESSIONS: dict[str, tuple[int, int]] = {
    "US30": (13, 20), "SPX500": (13, 20), "NAS100": (13, 20),   # US cash hours
    "XAUUSD": (7, 20),                                           # London + US
}


def resolve(broker: Broker, wanted: list[str]) -> tuple[dict[str, str], list[str]]:
    """Match requested names to what this account actually lists.

    Brokers decorate symbols (``XAUUSD.r``, ``US30cash``, ``NAS100_m``), so an
    exact-match-only survey silently reports nothing and looks like a broken
    connection rather than a naming mismatch.
    """
    available = broker.symbols()
    upper = {s.upper(): s for s in available}
    found, missing = {}, []
    for want in wanted:
        w = want.upper()
        if w in upper:
            found[want] = upper[w]
            continue
        hits = [orig for up, orig in upper.items() if up.startswith(w)]
        if hits:
            found[want] = sorted(hits, key=len)[0]
        else:
            missing.append(want)
    return found, missing


def run(
    broker: Broker,
    symbols: Optional[list[str]] = None,
    days: int = 90,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    now: Optional[datetime] = None,
) -> dict:
    """Measure everything and return the report as data, not text."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    wanted = symbols or DEFAULT_SYMBOLS

    resolved, missing = resolve(broker, wanted)
    frames, problems = fetch_all(broker, list(resolved.values()), start, now, M1)
    problems = [f"not offered on this account: {m}" for m in missing] + problems

    specs, points, session_hours = {}, {}, {}
    for want, actual in resolved.items():
        if actual not in frames:
            continue
        try:
            spec = broker.spec(actual)
        except KeyError:
            continue
        specs[actual] = spec
        # Spreads from MT5 are already in points, so the move must be too.
        points[actual] = spec.point
        if want in SESSIONS:
            session_hours[actual] = SESSIONS[want]

    table = costs.sweep(frames, horizons=horizons, points=points,
                        session_hours=session_hours)
    hours = {s: costs.spread_by_hour(df, points.get(s, 1.0))
             for s, df in frames.items()}
    return {
        "table": table,
        "lines": costs.verdict_lines(table),
        "spread_by_hour": hours,
        "specs": specs,
        "problems": problems,
        "window": (start, now),
    }


def format_report(result: dict) -> str:
    """The whole survey as text, suitable for a terminal or Telegram."""
    start, end = result["window"]
    out = [
        "COST WALL SURVEY",
        f"{start:%Y-%m-%d} to {end:%Y-%m-%d}  ({(end - start).days} days of M1 data)",
        "",
        "How to read this: 'ratio' is the typical move over the holding period",
        "divided by what a round trip costs. Below 2.0 the spread takes more",
        "than the trade is likely to make. 'breakeven_wr' is the win rate needed",
        "at 1:1 reward/risk -- compare it to the 54.05% that made Pocket Option",
        "unwinnable.",
        "",
    ]
    table = result["table"]
    if table.empty:
        out.append("No instrument returned usable data.")
    else:
        out.append(table.to_string(index=False))
    out += ["", "VERDICT", *[f"  - {ln}" for ln in result["lines"]]]

    if result["problems"]:
        out += ["", "COULD NOT MEASURE"]
        out += [f"  - {p}" for p in result["problems"]]

    worst = _widest_hours(result["spread_by_hour"])
    if worst:
        out += ["", "MOST EXPENSIVE HOURS (UTC, median spread in points)"]
        out += [f"  - {ln}" for ln in worst]
    return "\n".join(out)


def _widest_hours(by_hour: dict[str, pd.DataFrame], top: int = 3) -> list[str]:
    lines = []
    for symbol, df in sorted(by_hour.items()):
        if df.empty:
            continue
        ranked = df.sort_values("median", ascending=False).head(top)
        cheap = df["median"].min()
        parts = [f"{int(h):02d}:00 = {row['median']:.1f}" for h, row in ranked.iterrows()]
        lines.append(f"{symbol}: {', '.join(parts)}  (cheapest hour {cheap:.1f})")
    return lines


def format_check(diag: dict, found: dict[str, str], missing: list[str],
                 quotes: dict[str, tuple[float, float, float]]) -> str:
    """First-contact diagnostic: is this the right account, and can it trade?"""
    algo = "yes" if diag["algo_allowed"] else (
        'NO - tick "Algo Trading" in the terminal toolbar')
    experts = "yes" if diag["trade_expert"] else (
        "NO - the broker has not enabled automated trading on this account")
    out = [
        "CONNECTION CHECK",
        "",
        f"  terminal     {diag['terminal']}",
        f"  broker       {diag['company']}",
        f"  server       {diag['server']}",
        f"  account      {diag['login']}  ({diag['currency']}, 1:{diag['leverage']})",
        f"  balance      {diag['balance']:.2f}    equity {diag['equity']:.2f}",
        f"  connected    {'yes' if diag['connected'] else 'NO'}",
        f"  algo allowed {algo}",
        f"  EAs on acct  {experts}",
        "",
        "INSTRUMENTS",
    ]
    if quotes:
        out.append(f"  {'wanted':10s} {'broker symbol':18s} {'bid':>12s} {'ask':>12s} "
                   f"{'spread(pts)':>12s}")
        for want, (bid, ask, pts) in sorted(quotes.items()):
            out.append(f"  {want:10s} {found[want]:18s} {bid:12.5f} {ask:12.5f} {pts:12.1f}")
    else:
        out.append("  none of the requested instruments could be quoted")
    if missing:
        out += ["", "NOT OFFERED ON THIS ACCOUNT", *[f"  - {m}" for m in missing]]
    out += ["", "Spreads above are a single snapshot, not a measurement.",
            "Run without --check for the 90-day survey."]
    return "\n".join(out)


def check(broker, symbols: Optional[list[str]] = None) -> str:   # pragma: no cover
    diag = broker.diagnostics()
    found, missing = resolve(broker, symbols or DEFAULT_SYMBOLS)
    quotes = {}
    for want, actual in found.items():
        try:
            bid, ask = broker.quote(actual)
            point = broker.spec(actual).point
        except (KeyError, RuntimeError):
            missing.append(f"{want} (listed as {actual} but will not quote)")
            continue
        quotes[want] = (bid, ask, (ask - bid) / point if point else 0.0)
    return format_check(diag, found, missing, quotes)


def main(argv: Optional[list[str]] = None) -> int:      # pragma: no cover
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--login", type=int, default=None)
    ap.add_argument("--password", default="")
    ap.add_argument("--server", default="")
    ap.add_argument("--terminal", default=None, help="path to terminal64.exe")
    ap.add_argument("--out", default=None, help="write the report to this file too")
    ap.add_argument("--check", action="store_true",
                    help="connection and instrument check only; no history pulled")
    args = ap.parse_args(argv)

    broker = MT5Feed(login=args.login, password=args.password,
                     server=args.server, terminal_path=args.terminal)
    try:
        if args.check:
            text = check(broker, symbols=args.symbols)
        else:
            text = format_report(run(broker, symbols=args.symbols, days=args.days))
    finally:
        broker.close()
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(main())
