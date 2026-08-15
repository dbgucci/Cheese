# What 10,981 USDCAD OTC candles say

Data: the impulse studio's `scans` table, 7–15 August 2026, one bar a minute,
full OHLC plus the indicator values recorded at each bar. Every result below
searches only the first 65% of the window and is confirmed on the last 35%,
which the search never saw.

## The series is a random walk at one minute

| probe | result |
|---|---|
| Variance ratio q=2,5,10,30,60 | 1.01–1.08, every p > 0.24 |
| Autocorrelation, lags 1–60 | nothing survived multiplicity correction |
| Streaks, runs of 2–8 | flat; continuation and reversal both ~50% |
| Outsized bars (p90/p95/p99) | no reversion, no extension |
| Repeated return sequences | **zero** repeats in 7,000 bars at window 8 |
| Price quantisation | none; last digits uniform (chi2=4.4, p=0.88) |

109 hypotheses tested, 0 survived Benjamini-Hochberg, 0 reached the holdout.

## Its own indicators carry no information

Best training decile of each feature, replayed on the holdout:

| feature | train | holdout |
|---|---|---|
| ADX | 0.5298 | 0.4905 |
| EMA20 slope | 0.5418 | 0.5142 |
| range / ATR | 0.5312 | 0.5150 |
| close location | 0.5326 | 0.5093 |
| ATR | 0.5327 | 0.5088 |
| HA vs Keltner mid | 0.5319 | 0.5068 |
| price vs EMA200 | 0.5348 | 0.5396 |
| candle body | 0.5234 | 0.4803 |

Spearman rank correlation with the outcome: every feature between −0.003 and
+0.014, all p > 0.13.

Hour of day: the correlation between the training win rate for an hour and
its holdout win rate is **+0.022**. The good hours in the first two thirds of
the sample are unrelated to the good hours in the last third.

## Models find nothing either

Held out, never trained on:

| horizon | logistic AUC | random forest | gradient boosting |
|---|---|---|---|
| 1 min | 0.5036 | 0.5048 | 0.5052 |
| 2 min | 0.4899 | 0.5086 | 0.5043 |
| 3 min | 0.5040 | 0.5159 | 0.5075 |
| 5 min | 0.4939 | 0.5034 | 0.4870 |

Restricting to the model's most confident predictions does not help: the top
5% score 51.6% (n=190, p=0.36).

## The one thing that is real: the payout

Payout on this feed swings from 0.00 to 0.92, median 0.78, and 33% of bars
sit at the 0.92 maximum. Break-even moves with it, and the move is larger
than any edge measured anywhere in this project.

At the observed 54.03% win rate:

| payout | break-even | EV per unit staked |
|---|---|---|
| 0.78 (median) | 56.18% | −0.038 |
| 0.85 | 54.05% | −0.000 |
| 0.90 | 52.63% | +0.027 |
| 0.92 | 52.08% | **+0.037** |

The same win rate loses money at the median payout and makes money at the
maximum. The impulse studio already averages 0.9108, which is where its
+6.86 units came from -- not from the rules.

## How much the 54.03% is worth as evidence

114/211, 95% CI **[47.29%, 60.62%]**. At a 0.92 payout that CI spans
−0.092 to +0.164 per unit staked. The result is consistent with a strategy
that loses steadily and with one that is excellent.

Trades needed to establish it beats break-even at 80% power:

| if the true rate is | trades needed |
|---|---|
| 54% | ~5,500 |
| 55% | ~2,155 |
| 56% | ~1,139 |
| 58% | ~475 |
| 60% | ~258 |

At 1.95% of bars taken, 5,500 trades is roughly two years of continuous
running. That is the actual cost of proving a 54% edge at this sample rate.
