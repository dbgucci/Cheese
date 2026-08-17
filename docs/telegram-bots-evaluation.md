# Five Telegram bots, evaluated

`heikenashioriginal.py`, `heikenashinewlatest.py`, `heikenashinew.py`,
`heikenashiotc.py`, `srbot.py`.

**Verdict: no. Two of the five cannot report a result at all, one never
receives data, and the two strategies underneath score at chance. The
martingale then displays an ~87% win rate while losing money.**

Reproduce with:

```
pip install -e ".[research]"
python -m cheese_signals.research.heiken_ashi_eval
```

---

## 0. Revoke the credentials first

| secret | where | status |
|---|---|---|
| OANDA API key, `OANDA_DOMAIN = "live"` | 3 files | **live trading account** |
| OANDA account `001-001-16011137-001` | 3 files | exposed |
| Telegram bot token `7603758701:…` | 4 files | exposed |
| Telegram bot token `7824991930:…` | `srbot.py` | exposed |

A live OANDA key is not read-only. Rotate all four, then move them to
environment variables.

## 1. There are two strategies here, not five bots

The strategy blocks of all four Heiken Ashi files are **byte-identical**. They
differ only in data source, timeframe, and Telegram card formatting.

| file | source | TF | note |
|---|---|---|---|
| `heikenashioriginal.py` | OANDA | M5 | never resolves a trade (§2) |
| `heikenashinewlatest.py` | OANDA | M1 | the working variant |
| `heikenashinew.py` | OANDA | M1 | labels real pairs as `-OTC` (§3) |
| `heikenashiotc.py` | Pocket Option WS | M1 | subscribes to nothing (§4) |
| `srbot.py` | OANDA | M1/M5/M15 | different strategy |

## 2. The settlement measures a different trade from the one you place

```python
exec_attempt.expiry_at = add_tf_candles(exec_attempt.execute_at, sig.expiry_candles)
close_px = candle_close_at(df, a.expiry_at)
```

`candle_close_at` looks up the candle *starting* at `expiry_at`, so with
`EXPIRY_CANDLES = 1` the trade runs from the open of candle *k* to the close of
candle *k+1* — **two minutes**, while the card says "1 minute trade".

Every WIN/LOSS in the channel is scored on a different trade from the one the
reader was told to place. Pinned in
`test_the_two_settlement_conventions_are_different_trades`.

**On the M5 file this is fatal.** `next_aligned_minute` floors to the next
*minute*, with no timeframe alignment, so `execute_at` lands on an M5 candle
start only ~20% of the time — and `expiry_at = execute_at + 1 minute` then
never lands on one, because a minute after an M5 boundary is not an M5
boundary. `candle_close_at` returns `None` forever. `heikenashioriginal.py`
sends alerts and can never report a single result.

The martingale inherits the same offset: attempt 2 executes at `a.expiry_at`,
so attempts 1 and 2 **overlap by a candle**. They are not three independent
tries.

## 3. One bot analyses a different instrument from the one it tells you to trade

```python
def _fmt_pair_card(pair: str) -> str:
    # Visual match only: EUR_USD -> EURUSD-OTC (we are NOT changing your data source)
    return pair.replace("_", "") + "-OTC"
```

`heikenashinew.py` computes signals from **real OANDA EUR/USD** and prints them
as **`EURUSD-OTC`**. Pocket Option's OTC series is broker-generated and is not
the same series — that is the entire premise of the OTC study in this repo. The
analysis has no connection to the instrument being traded. The comment shows
the author knew; the card does not.

## 4. The OTC bot never receives a price

```python
SUBSCRIBE_MESSAGES = [
    # "42["subfor","NZDJPY_otc"]",
]
```

The list is empty, so `_on_open` subscribes to nothing and `_on_message` never
sees a tick. `get_candles` returns an empty frame, which fails
`len(df) < MIN_REQUIRED_CANDLES`, forever. Even with the subscribe line
uncommented it builds candles from ticks starting at launch, so it would need
120 minutes of uptime before its first possible signal, and 200 before the
EMA200 meant anything. `asyncio.run(asyncio.sleep(5))` in the reconnect handler
also raises rather than sleeping.

## 5. The strategies score at chance

Driftless random walks, 40,000 bars, 8 runs, ~2,692 signals each — a series
with no edge in it by construction:

| settlement | win rate | sd | EV @0.85 |
|---|---|---|---|
| 1 candle (what the card promises) | 0.4965 | 0.0137 | −0.0815 |
| 2 candles (what the code settles) | 0.4945 | 0.0112 | −0.0852 |

Break-even at a 0.85 payout is **0.5405**. The rules are unbiased — correct
behaviour — and there is nothing in them that clears the payout.

This is the same finding as the 10,981-candle OTC study in
`docs/otc-analysis-2026-08.md`: price-above-EMA200 scored 0.4995 out of sample
there, and the 1-minute horizon is a random walk (variance ratio 1.01–1.08,
every p > 0.24).

## 6. The confidence score

`confidence = body_score(0–40) + ema_score(0–30) + wick_score(0 or 30)`.

`is_strong_ha` is already a precondition of firing, so **`wick_score` is always
30**, and it also guarantees `body/range > 0.6`, so **`body_score` is always
≥ 24**. The floor is 54, not 0.

Worse, `ema_score` divides by a hardcoded **absolute** price distance:

```python
ema_score = min(ema_dist / 0.001, 1.0) * 30
```

On EURUSD (~1.08) that is 10 pips and the term is a real gradient. On USDJPY
(~150) it is a tenth of a pip and the term is pinned at its maximum. Measured:

| instrument | mean | sd | range |
|---|---|---|---|
| EURUSD-like (~1.08) | 79.9 | 11.52 | 54–100 |
| USDJPY-like (~150) | 90.8 | 4.86 | 56–100 |

The same number on the card means two different things depending on the pair,
and on yen pairs it barely varies. It is not a probability and was never
calibrated against one.

## 7. The martingale is the most misleading part

The bots announce WIN as soon as **any** attempt lands and only announce LOSS
after all three fail. So the channel displays the chance a *sequence* succeeds,
not the chance a trade wins:

| true win rate | channel shows | EV/sequence (1-2-4) | EV per unit staked |
|---|---|---|---|
| 50% | **87.50%** | −0.2250 | −0.0750 |
| 52% | 88.94% | −0.1095 | −0.0380 |
| 55% | 90.89% | +0.0474 | +0.0175 |

At a true 50% the channel shows **87.5% wins while losing 0.225 units per
sequence**. Martingale does not change expected value at all — the EV per unit
staked is exactly the house edge, which
`test_martingale_does_not_change_expected_value` asserts to 1e-9. It only moves
the losses into rarer, larger events, which is precisely what makes a losing
system look like a winning one for weeks.

## 8. `srbot.py`: its two filters do not filter

**Support/resistance.** It keeps every pivot in a 200-bar window and asks
whether price is within 0.2% of any of them:

| | |
|---|---|
| levels kept per window | 5.7 |
| P(near any support) | **0.953** |
| P(near any resistance) | **0.907** |

Since `compute_confidence` needs pattern (30) + S/R (40) to clear its own
`confidence < 70` gate, the gate reduces to "a pattern fired" — the S/R term is
nearly always granted.

**The star patterns.** The doji leg is written as an absolute threshold:

```python
if c1 < o1 and abs(c2 - o2) < 0.1 and c3 > o3 ...
```

0.1 is ~1000 pips on EURUSD. **100.00% of bars pass it.** Morning Star
collapses into "a down bar, any bar at all, then an up bar".

**`in_trend`** is `close[-1] > close[-5]` — a five-bar comparison used as a hard
directional filter.

## 9. Smaller things

- `new_high_low` mutates `last_highs` as a side effect inside an `or`, so it is
  skipped whenever the left operand is already true — the ratchet goes stale.
  It is also never reset, so it becomes an all-time high and stops firing.
- The live loop re-scans every 10 seconds against the **unclosed** current
  candle (OANDA's `complete` flag is never checked), so the "strong candle"
  test fires on a shape that often no longer exists once the bar closes.
- `floor_to_minute` *forces* `tzinfo=utc` rather than converting; harmless with
  `datetime.now(timezone.utc)`, a silent corruption with anything else.
- `srbot.resolve_and_martingale` re-fetches candles per pending signal per
  10-second loop across 5 pairs × 3 timeframes — an easy OANDA rate-limit.

---

## What is worth keeping

The Heiken Ashi premise — two trend candles, then a strong-bodied third, with
the trend — is a reasonable continuation setup, and it is close to what
`strategies.trend_following` already implements with an ADX regime gate. What
it is not is an edge at a 1-minute binary expiry on this data.

If any of it is worth continuing, in order:

1. **Rotate the credentials.**
2. **Fix the settlement off-by-one**, so the reported result is the trade
   actually placed. Until then no result from any of these bots means anything.
3. **Delete the martingale**, or report per-attempt results alongside per
   sequence. It is the reason a losing system looks like a winning one.
4. **Stop trading the 1-minute close-to-close horizon**, and **filter on
   payout** — the only variable in this project measured to move the outcome.
