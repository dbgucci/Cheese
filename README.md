# Cheese Signals

A short-timeframe (1-5 minute) trading signal engine, built to be more
disciplined than a typical "paste an RSI script" bot: it classifies market
regime first, only listens to the strategy suited to that regime, requires
a second strategy to agree before calling something a signal, and ships
with a backtester so you can see real numbers before risking money on it.

**Read "[Does this actually work?](#does-this-actually-work)" before you
point it at a live account.** The honest answer is: on the specific broker
this was built for (Pocket Option, OTC binary options), the payout
structure means a signal engine needs a real, durable edge just to break
even -- and short-timeframe price action is close enough to a random walk
that most retail signal systems don't clear that bar. This project is
built so you can measure that for yourself instead of taking anyone's word
for it, ours included.

## What's actually in here

- **`indicators.py`** -- RSI, EMA, MACD, Bollinger Bands, Stochastic, ATR,
  ADX. Hand-rolled on pandas/numpy, no TA-Lib build dependency.
- **`strategies.py`** -- three independent strategies, each gated to the
  regime it's designed for:
  - `trend_following`: EMA(9)/EMA(21) cross confirmed by MACD histogram,
    gated on ADX >= 20 (a trend has to actually be present).
  - `mean_reversion`: RSI + Bollinger %B extremes confirmed by a Stochastic
    K/D cross, gated on ADX <= 20 (only fires in range-bound conditions).
  - `price_action`: engulfing/pin-bar rejection candles at rolling
    support/resistance, regime-independent.
- **`confluence.py`** -- combines the regime strategy (trend or
  mean-reversion, whichever is active) with the price-action confirmation:
  agreement is rewarded, disagreement is penalized hard, and a solo vote is
  discounted versus two strategies agreeing. A higher-timeframe EMA(50/200)
  bias further discounts signals that fight the larger trend.
- **`backtest.py`** -- walk-forward simulation with no lookahead (candle
  *i* only ever sees `df.iloc[:i+1]`), reporting win rate, expectancy,
  profit factor, max drawdown, and the break-even win rate implied by your
  broker's payout ratio.
- **`risk.py`** -- fixed-fractional position sizing, a daily loss cutoff,
  an hourly trade cap, and an OTC low-liquidity session filter (weekends,
  overnight UTC). No martingale/anti-martingale staking -- see
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

| Strategy | Trades | Win rate | Net PnL (stakes) | Expectancy/trade | Profit factor |
|---|---|---|---|---|---|
| trend_following (solo) | _pending_ | | | | |
| mean_reversion (solo) | _pending_ | | | | |
| price_action (solo) | _pending_ | | | | |
| **confluence (combined)** | _pending_ | | | | |

Break-even win rate at an 85% payout is **54.1%** -- you need to be right
more than that just to not lose money; anything meaningfully above it is
where an edge would show up.

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
