"""CLI entrypoint: `backtest` a strategy over historical/synthetic data, or `watch` live candles."""

from __future__ import annotations

import argparse
import os
import sys
import time as time_mod
from datetime import datetime, timezone

import yaml

from . import confluence, risk, strategies
from .backtest import run_backtest
from .data import CsvFeed, SyntheticFeed, get_pocket_option_feed
from .data.synthetic import generate_synthetic_candles


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_feed(cfg: dict):
    source = cfg.get("data_source", "synthetic")
    if source == "synthetic":
        return SyntheticFeed(seconds_per_candle=cfg["timeframe_seconds"])
    if source == "csv":
        path = cfg.get("csv_path")
        if not path:
            raise ValueError("data_source: csv requires csv_path in config")
        return CsvFeed(path)
    if source == "pocket_option":
        return get_pocket_option_feed(cfg["asset"], timeframe_seconds=cfg["timeframe_seconds"])
    raise ValueError(f"unknown data_source: {source}")


def cmd_backtest(args: argparse.Namespace) -> None:
    if args.csv:
        df = CsvFeed(args.csv).get_candles(10 ** 9)
    else:
        df = generate_synthetic_candles(args.candles, seed=args.seed)

    print(f"Backtesting on {len(df)} candles ({df.index[0]} -> {df.index[-1]})\n")

    print("== Individual strategies (for comparison) ==")
    for name, fn in [
        ("trend_following", strategies.trend_following),
        ("mean_reversion", strategies.mean_reversion),
        ("price_action", strategies.price_action),
    ]:
        report = run_backtest(df, payout=args.payout, threshold=args.threshold, strategy_fn=fn)
        print(f"  {name:16s} {report.summary()}")

    print("\n== Confluence engine (combined) ==")
    report = run_backtest(df, payout=args.payout, threshold=args.threshold)
    print(f"  {'confluence':16s} {report.summary()}")

    if args.verbose and report.trades:
        print("\nLast 10 trades:")
        for t in report.trades[-10:]:
            side = "CALL" if t.direction == 1 else "PUT"
            outcome = "WIN " if t.won else "LOSS"
            print(f"  {t.timestamp}  {side:4s} {outcome} score={t.score:.2f}  {t.reason}")


def cmd_lab(args: argparse.Namespace) -> None:
    from . import strategy_lab as lab

    if args.csv:
        frames = {args.csv: CsvFeed(args.csv).get_candles(10 ** 9)}
    elif args.synthetic:
        frames = {"synthetic": generate_synthetic_candles(args.synthetic, seed=42)}
    else:
        frames = lab.load_journal_candles(args.asset)
        if not frames:
            print(
                "No candles recorded yet. Run the app with the engine on for a while, "
                "or pass --synthetic 5000 to try it on generated data."
            )
            return

    expiries = tuple(args.expiry) if args.expiry else (1, 2, 3, 4, 5)
    strategies = args.strategy or list(lab.STRATEGIES)

    for asset, df in sorted(frames.items()):
        if df is None or len(df) < 400:
            print(f"\n{asset}: only {0 if df is None else len(df)} candles -- need ~400+, skipping")
            continue

        print(f"\n=== {asset} — {len(df)} candles "
              f"({df.index[0]:%Y-%m-%d %H:%M} to {df.index[-1]:%Y-%m-%d %H:%M} UTC) ===")
        for e in expiries:
            print("  " + lab.baseline(df, e, payout=args.payout))
        print()

        results = lab.sweep(
            df, strategies=strategies, expiries=expiries,
            payout=args.payout, lead_minutes=args.lead, min_score=args.min_score,
        )
        for r in sorted(results, key=lambda r: -r.pnl):
            if r.trades:
                print("  " + r.line(args.payout))

        tradeable = [r for r in results if r.trades >= 30]
        if tradeable:
            best = max(tradeable, key=lambda r: r.pnl)
            print(
                f"\n  best with a usable sample: {best.strategy} at {best.expiry}min "
                f"({best.trades} trades, {best.win_rate:.1%}, {best.pnl:+.2f})"
            )
            if best.edge(args.payout) <= 0:
                print("  NOTE: even the best option is below break-even on this data.")
        else:
            print("\n  No combination reached 30 trades -- collect more candles first.")


def cmd_watch(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    feed = build_feed(cfg)

    notifier = None
    tg_cfg = cfg.get("telegram", {})
    if tg_cfg.get("enabled"):
        from .notifiers import TelegramNotifier

        token = tg_cfg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = tg_cfg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            print("Telegram enabled but bot_token/chat_id missing; disabling notifications", file=sys.stderr)
        else:
            notifier = TelegramNotifier(token, chat_id)

    risk_cfg = risk.RiskConfig(**cfg["risk"])
    risk_state = risk.RiskState()
    threshold = cfg["confluence"]["threshold"]
    bias_multiple = cfg.get("bias_multiple", 5)

    print(f"Watching {cfg['asset']} @ {cfg['timeframe_seconds']}s candles. Ctrl+C to stop.")
    last_signal_ts = None

    while True:
        df = feed.get_candles(max(300, 210 + 60))
        df_bias = df.resample(f"{bias_multiple * cfg['timeframe_seconds']}s").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()

        now = datetime.now(timezone.utc)
        if risk.is_low_liquidity_session(now):
            print(f"[{now:%H:%M:%S}] low-liquidity session (weekend/off-hours OTC) -- skipping")
            time_mod.sleep(cfg["poll_seconds"])
            continue

        result = confluence.evaluate(df, df_bias, threshold=threshold)
        latest_ts = df.index[-1]

        if result.is_signal and latest_ts != last_signal_ts:
            ok, why = risk.can_trade(risk_cfg, risk_state, now, result.score)
            side = "CALL (up)" if result.direction == 1 else "PUT (down)"
            msg = (
                f"{cfg['asset']} {side} | score {result.score:.2f} | "
                f"stake {risk.stake_size(risk_cfg)} | {result.describe()}"
            )
            if ok:
                print(f"[{now:%H:%M:%S}] SIGNAL: {msg}")
                if notifier:
                    notifier.send(msg)
                risk.record_trade(risk_state, 0.0)  # PnL is unknown until the option expires
                last_signal_ts = latest_ts
            else:
                print(f"[{now:%H:%M:%S}] signal suppressed by risk rules ({why}): {msg}")

        time_mod.sleep(cfg["poll_seconds"])


def main() -> None:
    parser = argparse.ArgumentParser(prog="cheese-signals")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="backtest on synthetic or CSV candle history")
    bt.add_argument("--csv", help="path to a CSV of historical candles")
    bt.add_argument("--candles", type=int, default=5000, help="synthetic candle count if --csv not given")
    bt.add_argument("--seed", type=int, default=42)
    bt.add_argument("--payout", type=float, default=0.85, help="broker payout ratio, e.g. 0.85 = 85%")
    bt.add_argument("--threshold", type=float, default=0.55)
    bt.add_argument("--verbose", action="store_true")
    bt.set_defaults(func=cmd_backtest)

    lab = sub.add_parser(
        "lab",
        help="compare strategies and expiries on your own recorded candles",
    )
    lab.add_argument("--asset", help="limit to one asset, e.g. EURUSD_otc")
    lab.add_argument("--csv", help="use a CSV instead of the journal")
    lab.add_argument("--synthetic", type=int, metavar="N",
                     help="use N synthetic candles instead of real data")
    lab.add_argument("--strategy", action="append",
                     help="strategy to test (repeatable); default: all")
    lab.add_argument("--expiry", type=int, action="append",
                     help="expiry in minutes (repeatable); default: 1-5")
    lab.add_argument("--payout", type=float, default=0.85)
    lab.add_argument("--lead", type=int, default=0,
                     help="advance-warning delay in minutes, as used live")
    lab.add_argument("--min-score", type=float, default=0.0)
    lab.set_defaults(func=cmd_lab)

    w = sub.add_parser("watch", help="poll a live/synthetic feed and emit signals")
    w.add_argument("--config", default="config.yaml")
    w.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
