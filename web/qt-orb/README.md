# ORB Autobot — browser overlay

A draggable panel that runs the opening-range breakout strategy **inside a
browser-based trading platform**, rather than through MetaTrader. Built for
[Liquid Charts](https://trader.liquidcharts.com/) (Liquid Brokers' platform, an
[FX Blue "Figaro"](https://www.fxbluelabs.com/figaro) white-label), which is
where the reference bot this was modelled on runs.

It shares its rules with the Python engine in
[`src/cheese_signals/markets/`](../../src/cheese_signals/markets/) — same
filters, same defaults, same refusal messages — because that side has the
walk-forward backtest behind it. If the two drift apart, the bot in the browser
is not the bot that was measured.

## Status: the strategy is done, the platform calls are not

**What works and is tested (88 tests, `npm test`):**

- the opening-range rules, ported from `orb.py` and asserted against the same
  cases as the Python suite
- DST-correct session opens via `Intl`, including the weeks when London and New
  York are four hours apart rather than five
- risk-based position sizing, rounding lots *down* onto the broker's step
- the circuit breakers: daily loss limit, consecutive-loss halt, max open
  positions, trades per day
- the panel, the session stats, CSV export, and the built bundle actually
  executing in a DOM and driving a full session against a mock account

**What is deliberately unfinished:** the eight calls in
[`src/adapter.js`](src/adapter.js) that touch the platform. They throw
`not wired up` rather than guessing. Liquid Charts' scripting API is documented
at `liquid-charts.gitbook.io`, which was unreachable from the machine this was
written on, and inventing a `SendOrder` signature produces code that looks
finished, fails at runtime, and can fail *after* sending a wrong order.

To finish it, open the trading page's console and run:

```js
copy(JSON.stringify(OrbAdapter.discoverPlatform(window), null, 2))
```

That is read-only — it enumerates globals and sends nothing. With that output
plus the Figaro doc pages for market data, `SendOrder`, the trade list, account
and symbol details, the adapter is mechanical to fill in.

## Build and use

```bash
npm test          # builds, then runs 88 tests
npm run build     # writes dist/orb-overlay.js
```

Then paste `dist/orb-overlay.js` into the platform's **Run Script** widget, or
into the browser console on the trading page. It is not minified on purpose:
this is a script that can place real orders, and being able to read it first is
worth more than the bytes.

It starts in **dry run**, with **Confirm every order** also on. Both have to be
turned off deliberately. Handles are on `window.ORB` for the console.

## Instruments

Type them exactly as your own chart shows them — `XAUUSD247`, `NAS100` — not a
nickname like "Gold". Broker suffixes are matched automatically, so `XAUUSD247`,
`XAUUSD.r` and `XAUUSD-ECN` all resolve to gold and get the London session.

An instrument with no session mapped is **refused, not guessed at**. Trading the
"opening range" of a market that was closed at the time is worse than not
trading.

## What the session counters do and don't mean

The panel counts trades **this bot managed since the page was loaded**. It is
not account history and it resets on reload — which is exactly why it says so on
the panel. Five trades at a 100% win rate is not a track record; it is five
trades. Export the CSV before closing the tab if you want to keep them.

## Bugs this found while being built

Worth listing, because each one would have reached a live account:

- **Concurrent cycles double-placed an order.** Every step awaits the platform
  and the loop polls on a timer, so one slow history call was enough for a
  second cycle to start, see no open position, and place a second order for the
  same breakout. `Bot.cycle` is now guarded against re-entry.
- **Sizing used the signal price, not the fill.** A long fills at the ask, so its
  stop is a spread further away than the plan assumed — real risk came in 2%
  over budget on a 250-point stop, and much worse on a tight one.
- **The bundle was a `SyntaxError` when pasted.** Every source declares
  `const api` at the end; concatenating them into one scope collided. Invisible
  to unit tests, which never load the bundle — hence `test/bundle.test.js`.
- **`document.readyState` was the wrong start gate.** A page can report
  `'loading'` with a body already present, and a hand-pasted script can arrive
  after `DOMContentLoaded` has fired — leaving a listener that never runs and a
  bot that silently never starts.

## Files

```
src/clock.js       session opens in exchange time zones, and symbol normalisation
src/strategy.js    the opening-range rules, ported from orb.py
src/adapter.js     the platform seam: interface, mock account, Figaro stub
src/bot.js         the loop, the circuit breakers, session stats and CSV
src/panel.js       the draggable overlay, styled to match the desktop apps
src/main.js        wiring, and the adapter discovery probe
build.js           concatenates src/ into one paste-able dist/orb-overlay.js
test/              88 tests: node --test
```
