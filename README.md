# Cheese Signals

A Windows desktop app that generates 1-minute trading signals for Pocket
Option **OTC** pairs, tells you the pair, direction, and the exact minute to
enter *minutes in advance*, then tracks every trade to expiry and logs why it
won or lost so the strategy can be tuned on real evidence.

![Live signals](docs/screenshots/live.png)

**Read "[Does this actually work?](#does-this-actually-work)" before you
point it at a live account.** The honest answer is: on Pocket Option's payout
structure a signal engine needs a real, durable edge just to break even, and
short-timeframe OTC price action is close enough to a random walk that most
retail signal systems don't clear that bar. This app is built so you can
measure that for yourself instead of taking anyone's word for it, mine
included.

## Features

- **Advance-warning signals** — announced *N minutes before* the entry
  candle, with the pair, BUY/SELL, exact entry time and expiry time. The
  setup is re-checked every candle in the meantime and cancelled if it
  breaks down.
- **OTC-tuned strategy engine** — a liquidity-sweep/exhaustion primary setup
  plus regime-gated trend and mean-reversion strategies, weighted for the
  fact that OTC feeds are broker-generated rather than real markets.
- **Every candle and every outcome is stored** in a SQLite journal in a
  folder on your Desktop, so you can re-analyse and re-backtest on your own
  broker's real data.
- **Win/loss reasons are recorded**, not just results — ADX, displacement
  quality, bias alignment, session, lead time — so the Analytics tab can
  show you *which conditions* actually make or lose money on your feed.
- **Telegram alerts** for both the advance signal and the post-expiry
  win/loss result.
- **Adjustable settings** in-app: lead time, expiry, confidence threshold,
  pairs, risk, sessions, data source, Telegram.
- **Modern dark UI**, and a single standalone `.exe` with no Python needed.

## Screenshots

| Analytics | History | Settings |
|---|---|---|
| ![Analytics](docs/screenshots/analytics.png) | ![History](docs/screenshots/history.png) | ![Settings](docs/screenshots/settings.png) |

## Getting the .exe

**Option A — download a prebuilt one.** Go to the repo's **Actions** tab →
**Build Windows EXE** → *Run workflow*. When it finishes, download the
`CheeseSignals-windows` artifact; it contains `CheeseSignals.exe`.

**Option B — build it yourself on Windows.** Clone the repo and
double-click `build_windows.bat`. It creates a virtualenv, installs
everything, and produces `dist\CheeseSignals.exe`.

Then copy the `.exe` anywhere (your Desktop is fine) and double-click it. On
first run it creates a **`CheeseSignals` folder on your Desktop** containing:

```
Desktop/CheeseSignals/
  signals.db        every candle, signal and outcome (SQLite)
  settings.json     your settings
  exports/          CSV exports from the History tab
  logs/
```

> The build runs on Windows because PyInstaller cannot cross-compile — a
> Linux machine cannot emit a Windows binary. The GitHub Actions workflow
> exists so you don't need a Windows machine to get one.

### Running from source (any OS)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[gui]"
python run_app.py
```

## How the advance warning works

This is the feature most likely to be misunderstood, so it's worth being
precise. Nothing predicts the future; a 1-minute setup is only confirmed
when a 1-minute candle *closes*. To give you real warning time, detection
and entry are separated:

```
detected 14:31:00  →  announced immediately  →  enter 14:33:00  →  expires 14:34:00
                          (lead = 2 min)
```

The honest trade-off: **the market keeps moving during the lead window, so a
longer warning is a weaker prediction.** Two things manage that:

1. The signal is **re-validated on every candle** until entry. If direction
   flips or confidence collapses, it's cancelled (and you get a cancellation
   message) before you ever place the trade.
2. Every signal records its lead time, and the Analytics tab has a **"By
   advance-warning time"** table showing realised win rate per lead bucket —
   so you can see exactly what the warning costs you and tune `lead_minutes`
   accordingly. Set it to `0` for immediate next-candle entry.

## The OTC question

Pocket Option OTC pairs are **not real markets**. Their quotes are generated
algorithmically from historical volatility models and run 24/7, independent
of any exchange. That has real consequences the strategy design accounts for
(see `profiles.py`):

- **Trend-following is demoted on OTC.** Real trends exist because real
  order flow persists. A volatility-model generator has no such mechanism,
  and synthetic feeds are widely reported to revert and repeat rather than
  trend durably.
- **Mean-reversion is favoured**, for the same reason.
- **The liquidity-sweep pattern is kept, but the usual rationale is
  dropped.** On real markets it works because institutions hunt resting stop
  orders. On OTC there are no resting stops to hunt, so "smart money" is
  simply the wrong explanation. The *shape* — wick through a level, close
  back inside, with ATR-confirmed displacement — remains a good exhaustion
  signal on a mean-reverting series, and that's the honest description.
- **Session filters are measured, not assumed.** "London/NY overlap
  liquidity" doesn't literally apply to a synthetic feed, so sessions are
  *tagged and analysed* rather than hardcoded as a filter. Turn session
  restriction on only once your own Analytics tab justifies it.

None of the above is settled fact about your particular feed. It's the
starting prior — and the journal exists so you can overturn it with data.

## What's actually in here

- **`indicators.py`** -- RSI, EMA, MACD, Bollinger Bands, Stochastic, ATR,
  ADX. Hand-rolled on pandas/numpy, no TA-Lib build dependency.
- **`strategies.py`** -- four strategies, each gated to the regime it's
  designed for:
  - `liquidity_sweep` *(primary 1-minute setup)*: price wicks through a
    confirmed pivot swing level and closes back **inside** it, with the
    rejection candle's body measured against ATR. All three conditions are
    required — a candle that *closes* beyond the level is a breakout (the
    opposite trade) and is explicitly rejected, and a sweep on a tiny
    indecisive body is filtered out as noise.
  - `trend_following`: EMA(9)/EMA(21) cross confirmed by MACD histogram,
    gated on ADX >= 20 (a trend has to actually be present).
  - `mean_reversion`: RSI + Bollinger %B extremes confirmed by a Stochastic
    K/D cross, gated on ADX <= 20 (only fires in range-bound conditions).
  - `price_action`: engulfing/pin-bar rejection candles at rolling
    support/resistance, regime-independent.
- **`profiles.py`** -- per-instrument strategy weighting. OTC demotes trend
  and favours reversion/exhaustion; live instruments trust trends normally.
- **`scheduler.py`** -- the advance-warning lifecycle: schedule an entry N
  minutes out, re-validate every candle, cancel on breakdown.
- **`storage.py`** -- SQLite journal of every candle, signal and outcome.
- **`outcome.py`** -- settles at expiry and attributes *why* it won or lost.
- **`analytics.py`** -- realised win rate sliced by strategy/session/pair/
  lead time/confidence, with sample-size-aware tuning suggestions.
- **`sessions.py`** -- session tagging; OTC-aware (OTC trades 24/7).
- **`engine.py`** -- the live loop wiring detection → alert → entry →
  expiry → journal, on a background thread.
- **`gui/`** -- the PySide6 desktop app (theme, widgets, four pages).
- **`confluence.py`** -- combines the regime strategy (trend or
  mean-reversion, whichever is active) with the price-action confirmation:
  agreement is rewarded, disagreement is penalized hard, and a solo vote is
  discounted versus two strategies agreeing. A higher-timeframe EMA(50/200)
  bias further discounts signals that fight the larger trend.
- **`backtest.py`** -- walk-forward simulation with no lookahead (candle
  *i* only ever sees `df.iloc[:i+1]`), reporting win rate, expectancy,
  profit factor, max drawdown, and the break-even win rate implied by your
  broker's payout ratio.
- **`risk.py`** -- fixed-fractional position sizing, a daily loss cutoff and
  an hourly trade cap. No martingale/anti-martingale staking -- see
  [why](#why-no-martingale) below.
- **`data/`** -- pluggable feeds: a synthetic regime-switching generator
  (works with zero setup, used for the backtests below), a CSV loader for
  real exported history, and an optional live Pocket Option adapter.
- **`notifiers/telegram.py`** -- push signals to a Telegram chat.
- **`bot.py`** -- CLI: `cheese-signals backtest` and `cheese-signals watch`.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Backtest against 8,000 synthetic 1-minute candles, no credentials needed:
cheese-signals backtest --candles 8000 --payout 0.85 --threshold 0.55 --verbose
```

To run against real history, export candles to CSV (`timestamp,open,high,low,close[,volume]`)
and run `cheese-signals backtest --csv path/to/candles.csv`.

To watch a live feed and get Telegram alerts:

```bash
cp config.example.yaml config.yaml   # edit asset/threshold/telegram as needed
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export POCKET_OPTION_SSID=...        # only if data_source: pocket_option
pip install binaryoptionstoolsv2     # only if data_source: pocket_option
cheese-signals watch --config config.yaml
```

`data_source: pocket_option` uses the unofficial
[binaryoptionstoolsv2](https://pypi.org/project/binaryoptionstoolsv2/)
WebSocket client. Pocket Option does not publish an official API or SDK, so
treat that adapter as best-effort: the reverse-engineered protocol can
change without notice. `data_source: synthetic` (the default) requires
nothing and is the safest way to try the bot end-to-end first.

## Backtest results

Run on the bundled synthetic generator, which explicitly alternates between
trending and range-bound regimes (real market data does this too, at
irregular and unpredictable intervals -- the synthetic generator just makes
the regime switches visible for testing). 1-minute candles, 1-minute expiry,
85% payout (Pocket Option's typical major-pair payout), 5 seeds x 6,000
candles each, aggregated:

| Strategy | Trades | Win rate | Net PnL (stakes) | Avg PnL/trade |
|---|---|---|---|---|
| trend_following (solo) | 3,130 | 57.1% | +175.95u | +0.056u |
| mean_reversion (solo) | 18 | 38.9% | -5.05u | -0.281u |
| price_action (solo) | 5,228 | 50.5% | -344.00u | -0.066u |
| **confluence (combined)** | 1,506 | 57.2% | +88.70u | +0.059u |

Break-even win rate at an 85% payout is **54.1%** -- you need to be right
more than that just to not lose money; anything meaningfully above it is
where an edge would show up.

A few things worth being honest about in this table:

- **`mean_reversion` and `price_action` are unprofitable standalone** on
  this synthetic data. `mean_reversion` barely fires at all (18 trades over
  30,000 candles across 5 seeds) because its ADX<=20 gate is strict and the
  synthetic generator spends most of its time in the trending regimes it
  was designed to switch into; the few trades it does take aren't a large
  enough sample to say much either way. `price_action` fires constantly
  (5,228 trades) and loses money doing it -- support/resistance rejection
  on its own isn't a real edge here.
- **`confluence` roughly matches `trend_following`'s per-trade edge (+0.059u
  vs. +0.056u) while taking about half as many trades** (1,506 vs. 3,130)
  and running noticeably smaller per-seed drawdowns in 4 of 5 seeds. That's
  the intended effect of requiring price-action to agree before boosting a
  trend signal, and of down-weighting (rather than blocking) trend signals
  that fight the higher-timeframe bias: it trades less often, but doesn't
  give up the edge to do it.
- **An earlier version of this same confluence engine did worse than
  `trend_following` traded alone** (net negative, then barely break-even) --
  first because it averaged all three strategies' votes as equals, and then
  because it let `price_action` fire as an independent trigger whenever no
  regime strategy had fired. Both were real bugs caught by running this
  exact backtest, not just eyeballing the code. See the commit history and
  `confluence.py`'s docstring for the specifics.
- **One seed (99) is net negative for both `trend_following` and
  `confluence`** (-34.85u and -21.35u respectively) even though the other
  four are solidly positive. That's the honest reason to run this over many
  seeds/windows rather than one: a single lucky backtest window will always
  exist somewhere, and it isn't evidence of a durable edge on its own.

Reproduce this yourself:

```bash
cheese-signals backtest --candles 6000 --seed <n> --payout 0.85 --threshold 0.55
```

Vary `--seed` and `--threshold` and look at how much the numbers move
around -- that variance is itself the most important finding: a handful of
backtest runs on a few thousand candles is not enough to conclude a
strategy has a durable edge, on synthetic data or (much more so) live.

## Does this actually work?

Three things worth knowing before you connect this to money:

1. **The payout math is the real opponent.** Binary options brokers
   typically pay ~70-90% on a win and take 100% on a loss. At an 85%
   payout you must win more than 54.1% of trades just to break even
   (`1 / (1 + payout)`). Most technical-indicator systems, backtested
   honestly, hover close to 50%. A few points of edge on paper can evaporate
   entirely once you account for the version of a strategy that got
   curve-fit to one backtest window.
2. **Pocket Option OTC prices are synthetic, not exchange-fed**, especially
   on weekends -- you are trading against your broker's own pricing engine,
   not a public market, which is a structural conflict of interest no
   indicator can filter out.
3. **Regulatory status varies by country** and binary options are
   restricted or banned for retail traders in several jurisdictions
   (including most of the EU, under ESMA rules). Check your local rules
   before trading real money on this platform.

None of that means the code here is useless -- multi-timeframe confluence
and regime-gating are real, standard techniques used well beyond binary
options (the same `strategies.py`/`confluence.py` logic works over CSV
forex/crypto history with no broker-specific code at all). It means: treat
every number this bot prints as a hypothesis to test on a demo account over
weeks, not a verdict, and definitely not a reason to size up.

## Why no martingale

A common "improvement" ChatGPT-style bots ship is doubling your stake after
a loss to "recover" it on the next win. That doesn't change your edge (or
lack of one) at all -- it just reshapes the same expected value into a
distribution with a small chance of a catastrophic loss streak wiping the
account. `risk.py` only implements fixed-fractional sizing (a constant % of
balance per trade) on purpose.

## Project layout

```
src/cheese_signals/
  indicators.py       technical indicators
  strategies.py       trend_following / mean_reversion / price_action
  confluence.py       regime-aware combination of the three
  backtest.py         walk-forward simulator + report
  risk.py             position sizing, trade pacing, session filters
  bot.py              CLI (backtest / watch)
  data/               synthetic, csv, pocket_option feeds
  notifiers/          telegram
tests/                pytest suite for indicators/strategies/backtest
config.example.yaml   copy to config.yaml for live `watch` mode
```

## Tests

```bash
pip install pytest
pytest
```
