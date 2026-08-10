// The opening-range rules, ported from src/cheese_signals/markets/orb.py.
//
// This is a port rather than a rewrite, and the distinction matters: the Python
// version is the one with a walk-forward backtest behind it, charging this
// broker's own spreads and checked against a driftless random walk to prove it
// is not peeking. If this file's rules drift from that file's rules, the bot
// running in the browser is not the bot that was measured, and the backtest
// stops meaning anything.
//
// So: same filters, same defaults, same refusal reasons, same order of checks.
// Pure functions over bars -- no DOM, no platform, no network -- which is what
// makes them testable outside a browser.
//
// Bars are `{ t, o, h, l, c, spread }` where `t` is a UTC epoch in
// milliseconds and stamps the bar's OPEN, matching the Python side.

'use strict';

const clock = (typeof require !== 'undefined')
  ? require('./clock.js')
  : globalThis.OrbClock;

const ENTRY_CLOSE = 'close';
const ENTRY_TOUCH = 'touch';
const STOP_RANGE_OPPOSITE = 'range_opposite';
const STOP_RANGE_FRACTION = 'range_fraction';

const BUY = 1;
const SELL = -1;

// Defaults are the textbook strategy, not a tuned one: a 15-minute range, a
// close-through entry, the stop at the opposite side, two units of reward per
// unit of risk, one trade a day, one direction a day.
const DEFAULTS = {
  rangeMinutes: 15,
  minRangeBarFraction: 0.8,
  entryMode: ENTRY_CLOSE,
  entryBufferPoints: 0,
  entryWindowMinutes: 120,
  maxTradesPerSession: 1,
  oneDirectionPerSession: true,
  stopMode: STOP_RANGE_OPPOSITE,
  stopFraction: 0.5,
  targetR: 2.0,
  breakevenAtR: 1.0,
  flatBeforeCloseMinutes: 10,
  minRangeCostMultiple: 3.0,
  minTargetCostMultiple: 3.0,
  minRangeAdrFraction: 0.05,
  maxRangeAdrFraction: 0.60,
  adrDays: 14,
};

function config(overrides) {
  return Object.assign({}, DEFAULTS, overrides || {});
}

// Settings that contradict each other, named rather than obeyed. A bot that
// never trades looks identical to a bot whose filters are inverted.
function validate(cfg) {
  const out = [];
  if (cfg.entryMode !== ENTRY_CLOSE && cfg.entryMode !== ENTRY_TOUCH) {
    out.push(`entryMode '${cfg.entryMode}' is not a mode`);
  }
  if (cfg.rangeMinutes < 1) out.push('rangeMinutes below 1 leaves no range');
  if (cfg.minRangeAdrFraction >= cfg.maxRangeAdrFraction) {
    out.push(`the range filter is inverted: a minimum of ${(cfg.minRangeAdrFraction * 100).toFixed(0)}% `
      + `of the average daily range is not below the maximum of `
      + `${(cfg.maxRangeAdrFraction * 100).toFixed(0)}%, so no day can qualify`);
  }
  if (cfg.targetR <= 0) out.push('targetR at or below 0 puts the take-profit behind the entry');
  if (cfg.breakevenAtR !== null && cfg.breakevenAtR >= cfg.targetR) {
    out.push(`breakevenAtR ${cfg.breakevenAtR} is not below targetR ${cfg.targetR}, `
      + `so the stop would only move as the trade closes anyway`);
  }
  if (cfg.maxTradesPerSession < 1) out.push('maxTradesPerSession below 1 disables the bot');
  if (cfg.entryWindowMinutes <= 0) {
    out.push('entryWindowMinutes at or below 0 closes the window before the range does');
  }
  return out;
}

function barredAfter(cfg, direction) {
  return cfg.oneDirectionPerSession ? [-direction] : [];
}

// --------------------------------------------------------------------------
// the opening range
// --------------------------------------------------------------------------
function median(values) {
  if (!values.length) return 0;
  const sorted = values.slice().sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

// The range over the first `rangeMinutes` of a session, or null if no bars fell
// inside it -- which is what an exchange holiday looks like, and is absence
// rather than an error.
function buildRange(bars, symbol, spec, localDate, cfg, point, opts) {
  const options = opts || {};
  const start = clock.sessionOpen(spec, localDate).getTime();
  const end = start + cfg.rangeMinutes * 60000;
  const window = bars.filter((b) => b.t >= start && b.t < end);
  if (!window.length) return null;

  let high = -Infinity;
  let low = Infinity;
  const spreads = [];
  for (const b of window) {
    if (b.h > high) high = b.h;
    if (b.l < low) low = b.l;
    if (typeof b.spread === 'number' && isFinite(b.spread)) spreads.push(b.spread);
  }

  // What a round trip costs, from the spreads actually quoted this morning
  // rather than a long-run average -- averaging hides the widened mornings, and
  // those are the ones a live bot trades through.
  let cost;
  if (options.costPoints !== undefined && options.costPoints !== null) {
    cost = options.costPoints;
  } else {
    const spread = spreads.length ? median(spreads) : (options.fallbackSpreadPoints || 0);
    cost = spread + (options.commissionPoints || 0);
  }

  return {
    symbol, localDate, start, end, high, low, point,
    bars: window.length,
    expectedBars: cfg.rangeMinutes,
    costPoints: cost,
    get width() { return this.high - this.low; },
    get widthPoints() { return point ? (this.high - this.low) / point : 0; },
    get mid() { return (this.high + this.low) / 2; },
  };
}

// Average daily range over the sessions strictly before `beforeKey`, in points.
//
// `before` is exclusive. Including the day being decided would let the filter
// see the session it is filtering, which is lookahead of the cheapest and most
// flattering kind -- and it is invisible, because a contaminated filter simply
// looks like a good filter.
function averageDailyRangePoints(bars, spec, point, days, beforeKey) {
  if (!bars.length || !point || days <= 0) return 0;
  const perDay = new Map();
  for (const b of bars) {
    const d = clock.sessionDate(spec, new Date(b.t));
    if (!clock.isWeekday(d)) continue;
    const key = clock.dateKey(d);
    if (key >= beforeKey) continue;
    // Session hours only: an index CFD quotes nearly round the clock, and its
    // 24-hour range includes hours no opening-range trade will ever be in.
    const open = clock.sessionOpen(spec, d).getTime();
    const close = clock.sessionClose(spec, d).getTime();
    if (b.t < open || b.t >= close) continue;
    const cur = perDay.get(key) || { high: -Infinity, low: Infinity };
    if (b.h > cur.high) cur.high = b.h;
    if (b.l < cur.low) cur.low = b.l;
    perDay.set(key, cur);
  }
  const keys = Array.from(perDay.keys()).sort().slice(-days);
  if (!keys.length) return 0;
  let total = 0;
  for (const k of keys) {
    const v = perDay.get(k);
    total += (v.high - v.low) / point;
  }
  return total / keys.length;
}

// Whether the day's range qualifies, and if not, exactly why not. Every
// refusal carries its numbers: "no trades today" without them is the most
// common way a bot is found to have been broken for a fortnight.
function checkRange(rng, cfg, adrPoints) {
  if (rng.bars < cfg.minRangeBarFraction * rng.expectedBars) {
    return { ok: false, reason: `only ${rng.bars} of ${rng.expectedBars} range bars arrived `
      + `-- a gap in the feed makes the high and low unreliable` };
  }
  if (rng.width <= 0) {
    return { ok: false, reason: 'the range has no width: every bar printed one price' };
  }
  const wall = cfg.minRangeCostMultiple * rng.costPoints;
  if (rng.widthPoints < wall) {
    return { ok: false, reason: `range ${rng.widthPoints.toFixed(0)}pt is under `
      + `${cfg.minRangeCostMultiple.toFixed(1)}x the ${rng.costPoints.toFixed(1)}pt round-trip `
      + `cost -- the spread would take more than the breakout is likely to give` };
  }
  if (adrPoints > 0) {
    const fraction = rng.widthPoints / adrPoints;
    if (fraction < cfg.minRangeAdrFraction) {
      return { ok: false, reason: `range is ${(fraction * 100).toFixed(1)}% of the `
        + `${adrPoints.toFixed(0)}pt average daily range, below the `
        + `${(cfg.minRangeAdrFraction * 100).toFixed(0)}% floor -- too quiet an open for the `
        + `break to mean anything` };
    }
    if (fraction > cfg.maxRangeAdrFraction) {
      return { ok: false, reason: `range is ${(fraction * 100).toFixed(1)}% of the `
        + `${adrPoints.toFixed(0)}pt average daily range, above the `
        + `${(cfg.maxRangeAdrFraction * 100).toFixed(0)}% ceiling -- most of a normal day's `
        + `travel happened before the range even closed` };
    }
  }
  if (rng.costPoints <= 0) {
    return { ok: true, reason: `range ${rng.widthPoints.toFixed(0)}pt at no modelled cost` };
  }
  return { ok: true, reason: `range ${rng.widthPoints.toFixed(0)}pt, `
    + `${(rng.widthPoints / rng.costPoints).toFixed(1)}x cost` };
}

function stopAndTarget(rng, cfg, direction, entry) {
  let stop;
  if (cfg.stopMode === STOP_RANGE_FRACTION) {
    const back = rng.width * cfg.stopFraction;
    stop = direction === BUY ? rng.high - back : rng.low + back;
  } else {
    stop = direction === BUY ? rng.low : rng.high;
  }
  const risk = Math.abs(entry - stop);
  const target = entry + cfg.targetR * risk * (direction === BUY ? 1 : -1);
  return { stop, target };
}

// The last moment a new position may be opened today: whichever comes first,
// the entry window expiring or the run-up to the close. A breakout twenty
// minutes before the bell has no time to travel two units of risk and will be
// flattened at the close anyway, so taking it pays a spread for a coin flip.
function entryDeadline(rng, cfg, spec) {
  const windowEnd = rng.end + cfg.entryWindowMinutes * 60000;
  const flat = clock.sessionClose(spec, rng.localDate).getTime()
    - cfg.flatBeforeCloseMinutes * 60000;
  return Math.min(windowEnd, flat);
}

// Does this CLOSED bar break the range, and in which direction.
//
// Stateless on purpose: whatever is stateful -- which directions are used up --
// is the caller's and is passed in, so the same function serves the live loop
// and any test.
function breakout(rng, bar, cfg, taken) {
  if (bar.t < rng.end) return null;              // still inside the range itself
  const barred = new Set(taken || []);
  const buffer = cfg.entryBufferPoints * rng.point;
  const upper = rng.high + buffer;
  const lower = rng.low - buffer;

  let longBreak;
  let shortBreak;
  let entry;
  if (cfg.entryMode === ENTRY_CLOSE) {
    longBreak = bar.c > upper;
    shortBreak = bar.c < lower;
    entry = bar.c;
  } else {
    longBreak = bar.h >= upper;
    shortBreak = bar.l <= lower;
    entry = null;
  }

  // Both sides taken out inside one minute. Which came first is not recoverable
  // from a bar's OHLC, and guessing awards the good half of every whipsaw.
  if (longBreak && shortBreak) return null;

  let direction;
  let level;
  if (longBreak && !barred.has(BUY)) { direction = BUY; level = upper; }
  else if (shortBreak && !barred.has(SELL)) { direction = SELL; level = lower; }
  else return null;

  const price = entry === null ? level : entry;
  const { stop, target } = stopAndTarget(rng, cfg, direction, price);
  // A stop on the wrong side means the range and the entry disagree; refusing
  // here keeps an impossible order away from the broker.
  if ((direction === BUY && stop >= price) || (direction === SELL && stop <= price)) {
    return null;
  }

  const side = direction === BUY ? 'above' : 'below';
  const verb = cfg.entryMode === ENTRY_CLOSE ? 'closed' : 'traded';
  return {
    symbol: rng.symbol, direction, entry: price, stop, target, at: bar.t, range: rng,
    reason: `${verb} ${side} the ${rng.widthPoints.toFixed(0)}pt opening range `
      + `(${rng.bars}-bar ${cfg.rangeMinutes}min range from `
      + `${clock.hhmm(new Date(rng.start))} UTC)`,
    get riskPoints() { return rng.point ? Math.abs(this.entry - this.stop) / rng.point : 0; },
    get rewardPoints() { return rng.point ? Math.abs(this.target - this.entry) / rng.point : 0; },
  };
}

// Whether the trade the rules produced is worth its own cost.
function checkPlan(plan, cfg) {
  if (plan.riskPoints <= 0) {
    return { ok: false, reason: 'the stop is at the entry: nothing to risk and nothing to size' };
  }
  const wall = cfg.minTargetCostMultiple * plan.range.costPoints;
  if (plan.rewardPoints < wall) {
    return { ok: false, reason: `target is ${plan.rewardPoints.toFixed(0)}pt against a `
      + `${plan.range.costPoints.toFixed(1)}pt round trip, under the `
      + `${cfg.minTargetCostMultiple.toFixed(1)}x floor` };
  }
  return { ok: true, reason: '' };
}

// The price at which the stop moves to entry, or null if never.
//
// Worth being clear about what this does: it removes the loss from a trade that
// worked and then stalled, and it also converts some eventual winners into
// scratches by stopping them out on a pullback they would have survived. Which
// effect dominates is instrument-specific and measurable, so it is a setting.
function breakevenStop(plan, cfg) {
  if (cfg.breakevenAtR === null || cfg.breakevenAtR === undefined) return null;
  const move = cfg.breakevenAtR * Math.abs(plan.entry - plan.stop);
  return plan.entry + move * (plan.direction === BUY ? 1 : -1);
}

// Lots such that the stop being hit costs `riskFraction` of equity.
//
// The only sizing rule. A stop twice as far away gets half the position, so the
// money at risk stays constant while volatility does not. Rounding is always
// DOWN onto the broker's step: rounding up places more risk than the policy
// allows, and below the minimum the answer is zero rather than the minimum,
// because silently trading a bigger position is how a "0.5% risk" bot risks 8%.
function sizeForRisk(equity, riskFraction, entry, stop, spec) {
  const distancePoints = Math.abs(entry - stop) / spec.point;
  if (!(distancePoints > 0) || !(equity > 0) || !(riskFraction > 0)) return 0;
  const perPointPerLot = spec.moneyPerPoint || 1;
  if (!(perPointPerLot > 0)) return 0;
  const raw = (equity * riskFraction) / (distancePoints * perPointPerLot);
  const step = spec.lotStep || 0.01;
  if (raw < spec.minLots) return 0;
  let lots = Math.floor(raw / step + 1e-9) * step;
  if (spec.maxLots) lots = Math.min(lots, spec.maxLots);
  const decimals = Math.max(0, -Math.floor(Math.log10(step)) + 2);
  lots = Number(lots.toFixed(decimals));
  return lots >= spec.minLots ? lots : 0;
}

const api = {
  BUY, SELL, ENTRY_CLOSE, ENTRY_TOUCH, STOP_RANGE_OPPOSITE, STOP_RANGE_FRACTION,
  DEFAULTS, config, validate, barredAfter,
  buildRange, averageDailyRangePoints, checkRange, checkPlan,
  stopAndTarget, entryDeadline, breakout, breakevenStop, sizeForRisk, median,
};

if (typeof module !== 'undefined' && module.exports) module.exports = api;
if (typeof globalThis !== 'undefined') globalThis.OrbStrategy = api;
