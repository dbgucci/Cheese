# The exhaustive strategy backtest: 19,706 configurations, 12.4 million trades

*Data: 90,618 one-minute bars, six OTC pairs, 4–22 August 2026, plus 1,549
settled live trades from the same account.*

This is the "just backtest everything" study. The earlier work in
`what-otc-actually-is.md` measured the *series* and found a random walk. The
obvious objection to that is that structure could still exist at the level of
a specific indicator, on a specific pair, at a specific hour. So this searched
that space to exhaustion: **102 strategy variants × 6 pairs × 24 hours ×
4 expiries = 19,706 testable configurations**, each scored on a holdout the
search never touched.

Every headline number was then re-derived on **24 independent universes of
Gaussian random walks** put through the identical pipeline, because the
benchmark for "my best cell hit 67%" is not 50% — it is what the same search
finds in data known to hold nothing.

The short version: the search found configurations winning **67%**. So did
the coins. None of it survived. And the live account settled the question
independently — it lost **exactly** what a coin at an 85% payout is supposed
to lose.

---

## 1. What was asked for, and what it looks like

The request was: best strategy, best pairs, best trading times. Here they
are, ranked in-sample, with the out-of-sample column that decides them.

### Best pairs

| pair | in-sample win% | out-of-sample win% |
|---|---|---|
| EURJPY_otc | 50.13 | 50.01 |
| USDCAD_otc | 50.02 | 50.07 |
| AUDUSD_otc | 50.01 | 49.84 |
| GBPUSD_otc | 49.99 | 49.95 |
| USDJPY_otc | 49.95 | 50.01 |
| EURUSD_otc | 49.91 | 50.19 |

The spread between the best and worst pair is **0.22 percentage points**.
There is no best pair.

### Best trading times

| UTC hour | in-sample win% | out-of-sample win% |
|---|---|---|
| 23 | 50.40 | 49.74 |
| 14 | 50.36 | 49.81 |
| 4 | 50.23 | 49.56 |
| 10 | 50.21 | 50.48 |
| 18 | 50.19 | 49.89 |
| … | … | … |
| 15 | 49.64 | 50.16 |

Correlation between the in-sample hourly ranking and the out-of-sample
ranking: **−0.302**. The hours that looked best went on to do slightly worse
than the hours that looked worst. There is no best hour.

### Best strategy configurations

The twenty best cells in the full grid:

| strategy | pair | hour | expiry | in-sample | out-of-sample |
|---|---|---|---|---|---|
| stoch14_10/90_follow | GBPUSD | 20 | 3m | **67.10%** | 47.62% |
| rsi2_10/90_revert | EURJPY | 1 | 5m | 66.25% | 42.73% |
| bb30_1.5_follow | GBPUSD | 20 | 3m | 66.01% | 40.32% |
| stoch14_20/80_revert | GBPUSD | 19 | 5m | 65.97% | 68.35% |
| always_call | USDJPY | 21 | 5m | 65.74% | 50.84% |
| … | | | | | |
| **mean of top 20** | | | | **64.44%** | **46.66%** |

---

## 2. Why none of that is an edge

### The measurement that matters

Ranking cells and quoting the best one measures the size of the search. The
honest question is whether in-sample rank predicts out-of-sample rank at all:

| statistic | real data | Gaussian random walks (3 universes) |
|---|---|---|
| correlation, in-sample → out-of-sample win rate | **−0.0454** | −0.013, +0.003, +0.000 |
| cells >55% in *both* halves | 315 | — |
| …chance predicts | 319 | — |
| ratio observed/chance | **0.99** | 1.07, 1.08, 1.21 |

Across 19,706 configurations, knowing which one won in-sample tells you
**nothing** about how it does next. The number of configurations that cleared
55% in both halves is 315 against a chance expectation of 319 — a ratio of
0.99. That is what independence looks like.

> **A note on the top-20 figure.** The 64.44% → 46.66% line above is a vivid
> illustration but a weak statistic, and it should not be quoted as the
> result. The top 20 cells occupy only 11 distinct pair/hour slots, so they
> are ~11 correlated bets, not 20 independent ones; its bootstrap 95%
> interval is [43.4%, 50.3%], which straddles 50%. On synthetic random walks
> the same figure ranged from 0.375 to 0.582 depending only on the seed. The
> full-grid correlation and the both-halves count are the stable statistics,
> and they are the ones this study rests on.

### What a coin produces when searched this hard

Running the same 19,706-cell search on data known to contain no edge:

| | best cell found |
|---|---|
| fair coin, same sample sizes | 64.22% (5th–95th pct: 62.58% – 66.48%) |
| **the real data** | **67.10%** |

The best configuration in eighteen days of real OTC candles is roughly what
searching noise this hard always finds.

### Walk-forward: what a bot would actually have earned

A bot does not get a holdout. It re-picks its strategy from history and lives
with the choice. Simulating exactly that, over 12 sequential folds:

| selection rule | selected at (in-sample) | delivered (forward) | trades |
|---|---|---|---|
| top 1, ≥150 trades | 68.70% | 57.44% | 242 |
| top 5, ≥150 trades | 67.85% | 52.56% | 1,524 |
| top 1, ≥400 trades | 63.24% | 43.28% | 238 |
| top 5, ≥400 trades | 61.58% | 43.56% | 1,313 |

Four defensible versions of the same idea, spanning **43% to 57%**. The
choice of an arbitrary selection parameter moves the result by fourteen
points.

That 57.44% is the one number in this study that looked like an edge, so it
was tested rather than argued away. The entire pipeline — signal generation,
19,706-cell grid, all four walk-forward rules — was re-run on **24 independent
universes of Gaussian random walks** matched to each pair's volatility and
segment structure. Coins, by construction, contain nothing.

| selection rule | real | coin mean | coin sd | coin range | p(coin ≥ real) |
|---|---|---|---|---|---|
| top 1, ≥150 | **57.44%** | 49.45% | 5.06 | 41.62 – 58.87 | **0.083** |
| top 5, ≥150 | 52.56% | 49.98% | 2.67 | 44.69 – 54.10 | 0.208 |
| top 1, ≥400 | 43.28% | 49.37% | 8.07 | **31.67 – 67.50** | 0.833 |
| top 5, ≥400 | 43.56% | 49.47% | 3.09 | 44.83 – 55.04 | 1.000 |

Every real number falls inside the distribution that coins produce. The
57.44% does not clear p<0.05 even taken on its own — and it was not obtained
on its own. It was the best of four rules, and the right benchmark is the
coin's *best of four*: mean 53.88%, range [47.71%, 67.50%], giving
**p = 0.167**.

Two details from that table are worth more than the p-values:

* A walk-forward on a **pure random walk** returned **67.50%**. Anyone who
  ran that one experiment and stopped would have a backtest showing a
  two-thirds win rate on a series with provably nothing in it.
* Within a single coin universe, the four rules spread by 8.5 points on
  average and up to 19.8. The real data's 14.2-point spread is unremarkable.
  In **58%** of coin universes, at least one of the four rules beat the
  52.08% break-even for a 92% payout.

That last figure is the practical warning. A bot that tries a handful of
selection rules and adopts whichever backtests best will clear break-even
more often than not **on data guaranteed to be worthless**.

---

## 3. The series itself, re-measured on 39% more data

The earlier study used 65,053 bars. This one has 90,618, and extends past its
19 August cutoff. Every measurement held.

**Returns are Gaussian.** Excess kurtosis after removing shared feed
glitches: −0.008, 0.048, 0.024, −0.045, −0.002, 0.041. Real FX runs 5–20+.
Jarque-Bera p-values 0.36–0.66: indistinguishable from normal.

**No memory.** Ljung-Box Q(20) p-values 0.28–0.92. Variance ratios at lags
2/5/15/30/60 all within |z| < 2.2 of 1.0 — a pure random walk at every
horizon.

**No sessions.** Busiest hour ÷ quietest hour, by true range:

| pair | ratio | ANOVA p |
|---|---|---|
| AUDUSD_otc | 1.07 | 0.63 |
| USDJPY_otc | 1.05 | 0.49 |
| USDCAD_otc | 1.07 | 0.42 |
| EURJPY_otc | 1.14 | 0.47 |
| EURUSD_otc | 1.21 | 0.64 |
| GBPUSD_otc | 1.35 | 0.44 |

Real FX runs 2–4× between the Tokyo lull and the London/New York overlap.
There is no London open here, and no reason to prefer any hour.

**No streaks.** Run lengths match a fair coin to within a few tenths of a
percent, and the next-bar up-rate after *k* consecutive same-direction bars
stays at 50% ± its standard error for every *k* from 1 to 5. Every
"after three reds, buy green" rule is answered by that one row.

**The pairs are independent.** Mean absolute same-minute correlation across
all fifteen pairings: **0.0069**. EURUSD and GBPUSD both contain USD; in a
real market they correlate +0.6 to +0.8. They do not share a dollar, because
there is no dollar.

**Drift does not persist.** This is the trap that makes a backtest look
alive. Over the whole sample EURUSD closed higher 51.8% of the time at a
15-minute horizon — apparently a 4.4-sigma effect. It is not: overlapping
windows re-count the same minutes and inflate the z by ~√N. Measured on
non-overlapping windows it is +0.9 sigma. And when the drift measured in the
first 65% of the data is traded in the last 35%:

| pair | trained direction | out-of-sample win rate |
|---|---|---|
| AUDUSD_otc | PUT | 50.57% |
| EURJPY_otc | PUT | 50.85% |
| EURUSD_otc | CALL | 46.02% |
| GBPUSD_otc | PUT | 51.42% |
| USDCAD_otc | PUT | 47.73% |
| USDJPY_otc | CALL | 47.29% |
| **mean** | | **48.98%** |

---

## 4. The live account: the theory, tested with money

The journal holds 1,549 settled trades at a flat 85% payout.

| | |
|---|---|
| settled trades | 1,549 |
| win rate | **48.74%** (95% CI ±2.49% — 50% is inside it) |
| break-even at 0.85 payout | 54.05% |
| **predicted** return on turnover at that win rate | **−9.83%** |
| **actual** return on turnover | **−9.84%** |
| net | **−$11,460** on $116,427 staked |
| max drawdown | $12,929 |
| longest losing streak | 9 |

The account returned −9.84% of turnover where the coin-flip arithmetic
predicts −9.83%. That is agreement to one part in a thousand, and it is the
strongest single result in this document: the model of this feed as a fair
coin minus the house fee predicted eighteen days of real P/L to within a
hundredth of a percentage point.

One detail worth naming: the win rate is roughly uniform across pairs
(47.0%–52.6%) but the P/L is not (−$4,770 to +$1,703). That gap is the
martingale. 39 trades at $1,049 — 2.5% of the trades — carried **35.2%** of
all money risked. Martingale does not change the expected value of a coin;
it concentrates the variance into the trades most able to hurt.

---

## 5. What follows for the bot

**On this feed, the search is finished.** Not "no edge found yet" — the
question is closed four independent ways, and then confirmed by the account
statement. Break-even at a 50% win rate requires a 100% payout. The best
payout observed on this feed is 92%. The gap is the house's fee, and no
arrangement of indicators, pairs or hours narrows it.

The levers that remain are real but all point the same direction:

| payout | EV per trade at 50% | per 1,000 trades at $50 |
|---|---|---|
| 0.60 | −0.200 | −$10,000 |
| 0.85 (this account) | −0.075 | −$3,750 |
| 0.92 (best seen) | −0.040 | −$2,000 |
| 1.00 | 0.000 | $0 |

Trading only at 0.92 turns a $3,750 loss per thousand into a $2,000 loss per
thousand. It reduces the bleed. It cannot reverse the sign.

**What the codebase gets out of this.** The value here is not a strategy;
it is the harness. `cheese_signals/research/strategy_grid.py` runs this whole
pipeline against any feed, and it is built so its negative answers can be
trusted:

* every signal is causal — entry is `open[t+1]`, verified by a test that
  perturbs future bars and asserts no earlier signal moves;
* a flat close is a refund, not a loss;
* returns are never computed across a collection gap;
* every rule appears in both polarities, so the grid cannot smuggle in a
  prior about direction;
* `synthetic_null()` re-runs the *entire* pipeline on volatility-matched
  random walks, so any headline number has a proper benchmark — this is what
  turned the one promising 57.44% into a measured p of 0.167;
* `walk_forward()` reports what an adaptive bot would actually have earned,
  which is the only backtest number a live bot should be held to.

And one habit is worth carrying into the bot regardless of the feed: **58%
of coin universes produced at least one selection rule that beat the 92%
break-even.** A bot that tries several rules and keeps whichever backtests
best is not selecting an edge, it is selecting a lucky draw. Any rule the bot
adopts should have to clear `synthetic_null()` first.

Point it at a market with real participants, real sessions and a cost wall a
real edge can clear. That work is in `cheese_signals/markets/`. On a 92%
payout over a coin, nothing can.
