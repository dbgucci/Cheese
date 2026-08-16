# The precision-reversal strategy, evaluated

Wick rejection + volume spike + EMA200 alignment + support/resistance, 1-candle
expiry, reporting 70–100% confidence.

**Verdict: no. Three of its four confluences were already measured on this
project's own 10,981 real OTC candles and none of them predicts anything; the
fourth is a relabelled volume flag; and the code has never run against this
repo's data at all.**

Reproduce anything below with:

```
pip install -e ".[research]"
python -m cheese_signals.research.precision_reversal ~/Desktop/KPS/signals.db --payout 0.92
python -m cheese_signals.research.precision_reversal --null 40000
```

---

## 1. It has never run

```python
ema200 = talib.EMA(close, timeperiod=200)
if direction == "BUY" and close.iloc[i] > ema200[i]:
```

`close` is a Series, so TA-Lib returns a Series, so `ema200[i]` is a **label**
lookup. Every candle frame in this project is indexed by timestamp, so the
label `500` does not exist and the line raises `KeyError: 500` the first time a
wick-rejection candle appears. Whatever produced the original results, it was
not this project's data.

Three further defects, none of which announce themselves:

| line | defect | effect |
|---|---|---|
| `if lower_wick > body * 1.5` first | order-dependent | a candle whose **upper** wick is four times larger is still called a BUY |
| same, when `body == 0` | `wick > 0` always passes | every doji is bullish |
| `volume[i-10:i]` | negative slice for `i < 10` | empty window, `mean()` is NaN, comparison silently False |
| `close[i-10:i+1]` | includes bar `i`; uses closes | the bar is compared against its own close, and a level made of closes ignores the highs and lows that define it |

## 2. There are not four confluences. There are three gates and a flag

Each of wick, trend and S/R does `return None` when it fails. So by the time
control reaches the final test:

```python
if len(confluences) >= 3:
    confidence = 70 + (len(confluences) - 3) * 10
```

`confluences` always holds at least 3 entries and at most 4. **The `>= 3` check
can never fail, and the confidence is only ever 70 or 80** — it is the volume
spike, renamed. The 90% and 100% branches are unreachable.

This is pinned in `tests/test_precision_reversal.py` rather than argued:
`test_confluence_count_can_never_be_below_three`,
`test_confidence_is_only_ever_70_or_80`.

The gates are not independent either. Over 119,800 bars:

| | probability |
|---|---|
| P(touched support) | 0.3953 |
| P(touched support \| bullish wick) | 0.5453 |

A long lower wick **is** a low print, so it largely creates the support touch it
is then counted as confirming. Requiring four things that measure the same
thing is not confluence; it is one condition counted four times.

## 3. Its components were already tested, on real OTC data

From `docs/otc-analysis-2026-08.md` — 10,981 USDCAD OTC candles, searched on the
first 65% and confirmed on the last 35%:

| this strategy's confluence | nearest measured feature | holdout |
|---|---|---|
| wick rejection | candle body | 0.4803 |
| wick rejection | close location in range | 0.5093 |
| EMA200 alignment | price above EMA200 | 0.4995 |
| S/R touch | Bollinger lower touch, revert | 0.4852 |
| S/R touch | Bollinger upper touch, revert | 0.4931 |

Volume was not in that battery, so it is untested rather than disproven — but it
is the one confluence the strategy treats as optional.

The horizon is the deeper problem. The expiry is **1 candle**, and at one minute
this series is a random walk: variance ratio 1.01–1.08 at every q, all p > 0.24;
no autocorrelation surviving multiplicity correction at any lag 1–60; 109
hypotheses tested, 0 survived. A later run of RSI, Bollinger and MACD across
3,326 threshold combinations found a best training result of 59.4% and **zero**
survivors out of sample.

## 4. What the rules score on nothing

60 independent driftless random walks, 20,000 bars each, ~1,093 signals per run
— a series with no edge in it by construction:

| | |
|---|---|
| win rate | mean **0.4988**, sd 0.0149 |
| range | 0.4712 – 0.5289 |
| runs clearing break-even @0.92 | **7 of 60 (11.7%)** |
| runs clearing break-even @0.78 | 0 of 60 |

The rules are unbiased — they extract nothing from noise, which is correct. The
number that matters is the second one: **at this signal count, one null run in
eight looks profitable at the maximum payout.** A single backtest reading 52–53%
is not evidence of an edge; it is the expected best of a handful of tries.

The repo's own `data.synthetic` generator demonstrates the trap directly. It
builds in drift regimes on purpose, and the repaired strategy scores 0.5245 on
it with p = 0.047 — apparently significant, comfortably inside the null spread
above, and produced by drift the generator was written to contain.

## 5. The confidence number is not calibrated

On the same runs, the tier the strategy labels more confident did worse:

| tier | drift-regime synthetic | repaired |
|---|---|---|
| 70% (no volume spike) | 0.5133 (n=1,921) | 0.5314 (n=828) |
| 80% (volume spike) | 0.5027 (n=935) | 0.5093 (n=377) |

Neither gap is significant. That is the point: there is no evidence the label
means anything, and "80% confidence" on a screen reads as an 80% chance of
winning. At a 0.92 payout the honest figure for both tiers is "about a coin
flip, minus the house edge".

## 6. The payout dominates everything

Observed payout on this feed swings 0.00–0.92, median 0.78. At the null win rate
of 49.88%:

| payout | break-even | EV per unit staked |
|---|---|---|
| 0.78 (median) | 56.18% | −0.112 |
| 0.85 | 54.05% | −0.077 |
| 0.90 | 52.63% | −0.052 |
| 0.92 | 52.08% | −0.042 |

Moving from the median payout to the maximum lowers the bar by **4.1 points of
win rate** (56.18% → 52.08%) and swings EV by +0.070 per unit staked — a larger
move than any edge measured anywhere in this project. Which payout you are
filled at matters more than every rule in the strategy combined.

## 7. What it would take to know

The rules fire on ~5.4% of bars. To establish at 80% power that a given true
win rate beats the 52.08% break-even:

| if the true rate is | trades | bars | days of continuous running |
|---|---|---|---|
| 54% | 4,194 | 77,582 | 54 |
| 55% | 1,809 | 33,464 | 23 |
| 56% | 1,002 | 18,535 | 13 |
| 58% | 438 | 8,102 | 6 |

That is the actual cost of proving this works, and it assumes a stationary edge
throughout.

---

## What is worth keeping

The premise is not stupid. Rejection at a level, aligned with a higher-timeframe
trend, is a real idea, and `strategies.liquidity_sweep` is the version of it
this repo already implements properly — it requires a *confirmed pivot* rather
than a rolling extreme, insists the wick pushes through the level while the body
closes back inside, and measures the rejection against ATR so an indecisive
candle is not a signal.

What this version adds on top of that is not edge:

- the confluence count is decorative,
- the confidence is a volume flag,
- the S/R test is largely implied by the wick test,
- and the horizon is the one the data says is unpredictable.

If it is worth pursuing, the two things to change are the ones the evidence
points at: **stop trading the 1-minute close-to-close horizon**, and **filter on
payout**, which is the only variable in this system measured to move the outcome.
