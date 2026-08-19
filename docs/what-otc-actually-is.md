# What Pocket Option OTC actually is

65,053 one-minute bars, six pairs, 4–19 August 2026. The study searched the
first 65% and confirmed on the last 35%. **740 hypotheses, none survived.**

That is the third negative result in a row, so this document does not report
another one. It reports what the data says the series *is* — which turns out
to be a complete answer, and closes the question.

## Four measurements

**1. The increments are Gaussian.**

| pair | kurtosis (raw) | after removing one glitch bar |
|---|---|---|
| EURUSD_otc | 5,644.4 | **0.03** |
| GBPUSD_otc | 8,313.1 | **−0.08** |
| USDJPY_otc | 0.07 | 0.07 |

A normal distribution has kurtosis 0. Real FX has 5–20+, because real markets
gap on news. Strip one corrupt bar per pair and these are *exactly* normal.

**2. Volatility does not vary by hour.**

| pair | quietest hour | busiest hour | ratio |
|---|---|---|---|
| AUDUSD_otc | 1.90 pips | 2.30 pips | 1.21× |
| EURUSD_otc | 1.60 | 2.00 | 1.25× |
| GBPUSD_otc | 2.80 | 3.40 | 1.21× |
| USDCAD_otc | 1.85 | 2.20 | 1.19× |
| EURJPY_otc | 2.70 | 3.50 | 1.30× |
| USDJPY_otc | 1.80 | 2.40 | 1.33× |

Real FX runs 2–4× between the Tokyo lull and the London/New York overlap.
These are flat. There is no London open, no New York session, no weekend.

**3. Run lengths match a fair coin to a fraction of a percent.**

Observed vs. coin, share of runs of each length:

| length | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| AUDUSD_otc | 50.3 / 50.0 | 25.1 / 25.0 | 12.7 / 12.5 | 5.9 / 6.2 | 3.0 / 3.1 | 1.6 / 1.6 |
| USDCAD_otc | 50.1 / 50.0 | 25.3 / 25.0 | 12.5 / 12.5 | 5.7 / 6.2 | 3.4 / 3.1 | 1.6 / 1.6 |

Every "after three reds, buy green" rule is answered here. There is no streak
behaviour to trade.

**4. The six pairs are statistically independent.**

Mean absolute same-minute correlation across all fifteen pairings: **0.0095.**

This is the decisive one. EURUSD and GBPUSD both contain USD, so in a real
market they correlate +0.6 to +0.8 — a dollar move moves both. Here they
correlate 0.009. They do not share a dollar because there is no dollar.

## So what it is

Six independent Gaussian random walks with constant volatility, running 24/7,
labelled with currency-pair names. Roughly:

```
price[t+1] = price[t] + normal(0, sigma_pair)
```

No memory, no sessions, no shared factors, no fat tails. Not a market with an
edge too small to find — a random number generator.

## Why no strategy can win

On a memoryless walk every entry rule wins exactly 50% of the time. The
payout then decides the outcome, and the arithmetic is closed:

| payout | EV per trade | per 1,000 trades at $50 |
|---|---|---|
| 0.60 | −0.200 | **−$10,000** |
| 0.78 (median) | −0.110 | −$5,500 |
| 0.85 | −0.075 | −$3,750 |
| 0.92 (best) | −0.040 | −$2,000 |
| **1.00** | 0.000 | $0 |

**Break-even at a 50% win rate requires a 100% payout. The maximum observed
on this feed is 92%.** The game is not hard to beat; it is arithmetically
unbeatable. The 8% is the house's fee for a coin flip.

Payout selection is still the largest lever available — trading only at 0.92
loses $2,000 per thousand instead of $5,500. It reduces the bleed. It cannot
reverse the sign.

## The 54.03% was a coin

The impulse studio's 114/211 has a 95% interval of **[47.29%, 60.62%]**, and
the probability of seeing it or better from a fair coin is **0.135**. Nothing
about it needs explaining.

How much evidence would be needed to tell 50% from a real edge:

| trades | a true 50% rate stays within |
|---|---|
| 211 | ±6.75% — anything under 56.7% proves nothing |
| 1,000 | ±3.10% |
| 5,000 | ±1.39% |
| 20,000 | ±0.69% |

## What this closes, and what it does not

Closed: prediction on Pocket Option OTC, at any timeframe, with any
indicator. Not "undiscovered" — structurally absent, and now measured four
different ways on 65,053 bars.

Not closed: real markets. A CFD on an index has actual participants, actual
sessions and actual published evidence behind intraday momentum. That work
is in `cheese_signals/markets/`, and the cost wall there is a real bar that a
real edge can clear — unlike a 92% payout on a coin, which nothing can.
