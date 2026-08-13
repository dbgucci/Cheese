"""Exhaustive analysis of the recorded OTC candle history.

The premise that shapes everything here: **Pocket Option OTC prices are
generated, not traded.** They run 24/7, they have no order book, and no
exchange publishes them. So the usual reasons a market is predictable --
order flow, liquidity provision, information arriving -- do not apply, and
searching for them is searching in the wrong place.

What *can* be true of a generated series is different, and testable:

* it may mean-revert, because a model drawn around a level does;
* it may repeat, because a generator with limited state eventually must;
* it may have a periodic volatility profile, because the parameters driving
  it are usually scheduled;
* it may leak its own structure -- quantised prices, bounded ranges,
  correlated innovations across pairs sharing one generator.

Any of those is a real, exploitable edge. None of them is an indicator.

So this package looks for the generator rather than for chart patterns, and
holds every finding to the same standard the earlier work in this project
failed: measured out of sample, corrected for the number of things tried,
and compared against the drift on the same windows rather than against zero.
"""
