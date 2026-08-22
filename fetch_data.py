"""Download intraday bars into a csv the backtest can read.

    python fetch_data.py                     -> Nasdaq 100, 5-minute, 60 days
    python fetch_data.py --symbol ^GSPC
    python fetch_data.py --interval 15m --period 60d
    python fetch_data.py --interval 1h --period 730d

Yahoo caps intraday history hard: 5-minute data goes back 60 days and no
further, 15-minute the same, hourly two years. Sixty days of 5-minute bars is
roughly 4,700 rows once US cash hours are accounted for -- enough for a first
look, not enough to conclude anything, and the backtest's holdout split will
say so.

Two differences from the instrument in the recordings, both worth knowing
before reading any result as transferable:

* ^NDX is the cash index, quoted during US regular hours only. A NAS100 CFD
  trades nearly around the clock, so its daily high/low -- and therefore every
  pivot level built from them -- are not the same numbers.
* There is no spread or financing in index data. The backtest charges the
  1.10-point spread observed on the real ticket, which is the right order of
  magnitude for the CFD but is an assumption, not a measurement of your fills.

For history that actually matches a CFD, export it from the platform you would
trade on -- MetaTrader 5's History Center will give years of M5 -- and point
the backtest at that file instead. The loader reads MetaTrader's format.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="^NDX")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--period", default="60d")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    try:
        import yfinance as yf
    except ImportError:
        sys.exit("this needs yfinance:\n    python -m pip install yfinance")
    import pandas as pd

    out = args.out or f"{args.symbol.lstrip('^')}_{args.interval}.csv"
    print(f"downloading {args.symbol} {args.interval} over {args.period} ...")
    df = yf.download(args.symbol, interval=args.interval, period=args.period,
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        sys.exit(f"no data came back for {args.symbol} at {args.interval}/"
                 f"{args.period}.\nYahoo refuses intervals below 1d beyond its "
                 f"window -- 5m and 15m stop at 60d, 1h at 730d.")

    # yfinance returns a MultiIndex on the columns for a single ticker too.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)

    need = ["open", "high", "low", "close"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        sys.exit(f"download is missing {missing}; got {list(df.columns)}")

    keep = need + (["volume"] if "volume" in df.columns else [])
    df = df[keep].dropna(subset=need)
    df.index.name = "ts"
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df.to_csv(out)

    span = df.index[-1] - df.index[0]
    print(f"wrote {out}: {len(df):,} bars, {df.index[0]} .. {df.index[-1]} "
          f"({span.days} days)")
    print(f"\nnow run:\n    python run_pivot.py {out}")
    if len(df) < 20000:
        print(f"\n{len(df):,} bars is a small sample. Treat anything it says as "
              f"a smoke test,\nnot a result -- check the holdout column, not "
              f"the training one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
