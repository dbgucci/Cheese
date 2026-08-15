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

---

# RSI, Bollinger Bands and MACD, tested directly

The battery above tested the series and the impulse strategy's own features.
It did not test the three most widely used indicators, so this does, across a
grid of thresholds rather than one conventional setting -- and then across
every pair and triple of the strongest conditions, because the usual defence
of an indicator that fails alone is that it needs confluence.

**3,326 hypotheses. Best training result 59.4%. Zero survived multiplicity
correction. Every one of the top 25 collapsed on the holdout.**

| rule | train | holdout |
|---|---|---|
| bb50 + bb14 upper break + above EMA200 | 0.5940 (n=266) | 0.5115 (n=131) |
| bb20 + bb50 lower revert + RSI21<40 | 0.5899 (n=178) | 0.4848 (n=99) |
| bb20 + RSI9<30 + bb50 lower revert | 0.5838 (n=173) | 0.5000 (n=96) |
| bb20 + bb14 squeeze + MACD hist up | 0.5796 (n=157) | 0.5352 (n=71) |
| bb20 + bb14 squeeze down + below EMA200 | 0.5767 (n=189) | 0.4074 (n=108) |

## The textbook rules on their own

| rule | train | holdout | p(>50%) |
|---|---|---|---|
| RSI14 < 30 | 0.5274 | 0.5046 | 0.47 |
| RSI14 > 70 | 0.4636 | 0.4725 | 0.79 |
| RSI7 < 25 | 0.5098 | 0.4951 | 0.59 |
| RSI21 < 30 | 0.5375 | 0.4530 | 0.87 |
| Bollinger lower touch, revert | 0.5394 | 0.4852 | 0.70 |
| Bollinger upper touch, revert | 0.4863 | 0.4931 | 0.61 |
| Bollinger squeeze, up | 0.5152 | 0.4528 | 0.97 |
| MACD cross up | 0.4558 | 0.4397 | 0.94 |
| MACD histogram up | 0.5060 | 0.5205 | 0.04 |
| Price above EMA200 | 0.5098 | 0.4995 | 0.53 |
| EMA9 over EMA21 | 0.5050 | 0.4974 | 0.60 |

**Mean holdout win rate across every textbook rule: 0.4875.** One of
twenty-four clears the 52.08% break-even, and that one (MACD histogram up,
0.5205) is *below* it.

Across all 127 single rules the correlation between training and holdout win
rate is +0.228, mean training 0.5007 against mean holdout 0.4940. Indicators
do not merely fail to predict this series; picking the ones that looked best
in training makes the holdout slightly worse than picking at random, which is
the signature of fitting noise.
