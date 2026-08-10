// The ported rules, asserted against the same cases as the Python suite.
//
// The point of duplicating them is to catch a port that drifted. A rule that
// behaves differently here from tests/test_markets_orb.py means the browser bot
// is not the bot the backtest measured.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const clock = require('../src/clock.js');
const orb = require('../src/strategy.js');

const US = clock.SESSIONS.us_cash;
const DAY = { year: 2026, month: 3, day: 2 };        // Monday, US winter
const OPEN = Date.UTC(2026, 2, 2, 14, 30);           // 09:30 EST
const POINT = 0.1;                                   // US30-like

function bars(rows, start, spread) {
  const t0 = start === undefined ? OPEN : start;
  const sp = spread === undefined ? 10 : spread;
  return rows.map((r, i) => ({
    t: t0 + i * 60000, o: r[2], h: r[0], l: r[1], c: r[2], spread: sp,
  }));
}

// A clean 15-bar range: 20 index points = 200 broker points.
function rangeThen(rows, opts) {
  const o = opts || {};
  const high = o.high === undefined ? 44010 : o.high;
  const low = o.low === undefined ? 43990 : o.low;
  const head = [];
  for (let i = 0; i < 15; i += 1) head.push([high, low, 44000]);
  return bars(head.concat(rows || []), undefined, o.spread);
}

const CFG = orb.config();

function makeRange(opts) {
  return orb.buildRange(rangeThen([], opts), 'US30', US, DAY, CFG, POINT, {});
}

// ------------------------------ the range ------------------------------
test('the range is the high and low of the first N minutes', () => {
  const frame = rangeThen([[44100, 44050, 44090], [44100, 44050, 44090]]);
  const rng = orb.buildRange(frame, 'US30', US, DAY, CFG, POINT, {});
  assert.equal(rng.high, 44010);
  assert.equal(rng.low, 43990);
  assert.equal(rng.bars, 15);
});

test('the range window excludes the bar that could trigger it', () => {
  const rng = makeRange();
  assert.equal(rng.start, OPEN);
  assert.equal(rng.end, OPEN + 15 * 60000);
});

test('a session with no bars is absence, not an error', () => {
  const rng = orb.buildRange(rangeThen([]), 'US30', US,
    { year: 2026, month: 3, day: 3 }, CFG, POINT, {});
  assert.equal(rng, null);
});

test('width is reported in broker points', () => {
  const rng = makeRange();
  assert.equal(rng.width.toFixed(4), '20.0000');
  assert.equal(Math.round(rng.widthPoints), 200);
  assert.equal(rng.mid, 44000);
});

test('the cost comes from the range own spreads', () => {
  const frame = rangeThen([], { spread: 30 });
  const rng = orb.buildRange(frame, 'US30', US, DAY, CFG, POINT, { commissionPoints: 5 });
  assert.equal(rng.costPoints, 35);
});

// ------------------------------ the filters ------------------------------
test('a range narrower than the cost wall is refused with its numbers', () => {
  const rng = makeRange({ high: 44001, low: 44000 });
  const check = orb.checkRange(rng, CFG, 500);
  assert.equal(check.ok, false);
  assert.match(check.reason, /round-trip cost/);
  assert.match(check.reason, /10\.0pt/);
});

test('a range too quiet against the daily range is refused', () => {
  const rng = makeRange();
  const check = orb.checkRange(rng, orb.config({ minRangeAdrFraction: 0.5 }), 1000);
  assert.equal(check.ok, false);
  assert.match(check.reason, /too quiet/);
});

test('a range that already covered the day is refused', () => {
  const rng = makeRange();
  const check = orb.checkRange(rng, orb.config({ maxRangeAdrFraction: 0.1 }), 1000);
  assert.equal(check.ok, false);
  assert.match(check.reason, /before the range even closed/);
});

test('a gappy range is refused because its extremes are unreliable', () => {
  const frame = bars([[44010, 43990, 44000], [44010, 43990, 44000]]);
  const rng = orb.buildRange(frame, 'US30', US, DAY, CFG, POINT, {});
  assert.equal(orb.checkRange(rng, CFG, 1000).ok, false);
});

test('an ordinary range passes and says how far over the wall it is', () => {
  const check = orb.checkRange(makeRange(), CFG, 1000);
  assert.equal(check.ok, true);
  assert.match(check.reason, /20\.0x cost/);
});

test('no daily-range history disables that filter rather than blocking', () => {
  assert.equal(orb.checkRange(makeRange(), CFG, 0).ok, true);
});

// ------------------------------ the breakout ------------------------------
function barAt(minutesAfterOpen, h, l, c) {
  return { t: OPEN + minutesAfterOpen * 60000, o: c, h, l, c, spread: 10 };
}

test('a close above the range is a long', () => {
  const plan = orb.breakout(makeRange(), barAt(15, 44020, 44005, 44015), CFG, []);
  assert.equal(plan.direction, orb.BUY);
  assert.equal(plan.entry, 44015);
  assert.equal(plan.stop, 43990);
});

test('a close below the range is a short', () => {
  const plan = orb.breakout(makeRange(), barAt(15, 43995, 43980, 43985), CFG, []);
  assert.equal(plan.direction, orb.SELL);
  assert.equal(plan.stop, 44010);
});

test('a wick through the level that closes back inside is not a breakout', () => {
  assert.equal(orb.breakout(makeRange(), barAt(15, 44030, 44000, 44005), CFG, []), null);
});

test('touch mode takes that same wick, at the level', () => {
  const cfg = orb.config({ entryMode: orb.ENTRY_TOUCH });
  const plan = orb.breakout(makeRange(), barAt(15, 44030, 44000, 44005), cfg, []);
  assert.equal(plan.direction, orb.BUY);
  assert.equal(plan.entry, 44010);
});

test('a bar still inside the range window cannot trigger', () => {
  assert.equal(orb.breakout(makeRange(), barAt(5, 44100, 44050, 44090), CFG, []), null);
});

test('a bar that takes out both sides is refused rather than guessed', () => {
  const cfg = orb.config({ entryMode: orb.ENTRY_TOUCH });
  assert.equal(orb.breakout(makeRange(), barAt(15, 44050, 43950, 44030), cfg, []), null);
});

test('a direction already used is not traded again', () => {
  const bar = barAt(15, 44020, 44005, 44015);
  assert.equal(orb.breakout(makeRange(), bar, CFG, [orb.BUY]), null);
});

test('the entry buffer requires travel past the level', () => {
  const bar = barAt(15, 44012, 44005, 44011);
  assert.notEqual(orb.breakout(makeRange(), bar, CFG, []), null);
  assert.equal(orb.breakout(makeRange(), bar, orb.config({ entryBufferPoints: 50 }), []), null);
});

// ------------------------------ stops and targets ------------------------------
test('the default stop is the far side of the range and the target is 2R', () => {
  const { stop, target } = orb.stopAndTarget(makeRange(), CFG, orb.BUY, 44015);
  assert.equal(stop, 43990);
  assert.equal(target, 44015 + 2 * 25);
});

test('the range-fraction stop sits inside the range', () => {
  const cfg = orb.config({ stopMode: orb.STOP_RANGE_FRACTION });
  const { stop } = orb.stopAndTarget(makeRange(), cfg, orb.BUY, 44015);
  assert.equal(stop, 44000);
});

test('the short side mirrors', () => {
  const { stop, target } = orb.stopAndTarget(makeRange(), CFG, orb.SELL, 43985);
  assert.equal(stop, 44010);
  assert.equal(target, 43985 - 2 * 25);
});

test('a target too small to pay for the round trip is refused', () => {
  const rng = makeRange({ high: 44002, low: 44000, spread: 30 });
  const plan = orb.breakout(rng, barAt(15, 44003, 44001, 44002.5), CFG, []);
  assert.equal(Math.round(plan.rewardPoints), 50);
  const check = orb.checkPlan(plan, CFG);
  assert.equal(check.ok, false);
  assert.match(check.reason, /round trip/);
});

test('the breakeven trigger is R multiples from the entry', () => {
  const plan = orb.breakout(makeRange(), barAt(15, 44020, 44005, 44015), CFG, []);
  assert.equal(orb.breakevenStop(plan, CFG), 44040);
  assert.equal(orb.breakevenStop(plan, orb.config({ breakevenAtR: null })), null);
});

test('the entry window closes before the session does', () => {
  const rng = makeRange();
  const deadline = orb.entryDeadline(rng, orb.config({ entryWindowMinutes: 600 }), US);
  assert.equal(deadline, clock.sessionClose(US, DAY).getTime() - 10 * 60000);
});

// ------------------------------ the daily-range yardstick ------------------------------
test('the daily-range average excludes the day being decided', () => {
  // Three sessions, widths 10, 20 and 90 index points.
  const frames = [];
  const days = [{ year: 2026, month: 3, day: 2 }, { year: 2026, month: 3, day: 3 },
    { year: 2026, month: 3, day: 4 }];
  const widths = [10, 20, 90];
  days.forEach((d, i) => {
    const start = clock.sessionOpen(US, d).getTime();
    frames.push({ t: start, o: 44000, h: 44000 + widths[i], l: 44000, c: 44000, spread: 10 });
    frames.push({ t: start + 60000, o: 44000, h: 44000, l: 44000, c: 44000, spread: 10 });
  });
  const adr = orb.averageDailyRangePoints(frames, US, POINT, 14, clock.dateKey(days[2]));
  assert.equal(Math.round(adr), Math.round((10 + 20) / 2 / POINT));
});

test('the first session has no yardstick behind it', () => {
  const d = { year: 2026, month: 3, day: 2 };
  const start = clock.sessionOpen(US, d).getTime();
  const frame = [{ t: start, o: 44000, h: 44010, l: 43990, c: 44000, spread: 10 }];
  assert.equal(orb.averageDailyRangePoints(frame, US, POINT, 14, clock.dateKey(d)), 0);
});

// ------------------------------ config and sizing ------------------------------
test('the defaults have nothing to complain about', () => {
  assert.deepEqual(orb.validate(orb.config()), []);
});

test('an inverted range filter is named rather than obeyed', () => {
  const problems = orb.validate(orb.config({ minRangeAdrFraction: 0.8, maxRangeAdrFraction: 0.2 }));
  assert.ok(problems.some((p) => /inverted/.test(p)));
});

test('a breakeven at or beyond the target is pointless and says so', () => {
  const problems = orb.validate(orb.config({ breakevenAtR: 2.0, targetR: 2.0 }));
  assert.ok(problems.some((p) => /breakevenAtR/.test(p)));
});

test('one direction per session bars the opposite side', () => {
  assert.deepEqual(orb.barredAfter(orb.config(), orb.BUY), [orb.SELL]);
  assert.deepEqual(orb.barredAfter(orb.config({ oneDirectionPerSession: false }), orb.BUY), []);
});

test('a wider stop gets a smaller position', () => {
  const spec = { point: 0.00001, minLots: 0.01, lotStep: 0.01, maxLots: 100, moneyPerPoint: 1 };
  const near = orb.sizeForRisk(10000, 0.01, 1.09, 1.089, spec);   // 100 points
  const far = orb.sizeForRisk(10000, 0.01, 1.09, 1.088, spec);    // 200 points
  assert.ok(Math.abs(far - near / 2) < 0.02, `${near} vs ${far}`);
});

test('below the minimum lot the answer is zero, not the minimum', () => {
  const spec = { point: 0.1, minLots: 0.1, lotStep: 0.1, maxLots: 50, moneyPerPoint: 0.1 };
  assert.equal(orb.sizeForRisk(50, 0.005, 44000, 43900, spec), 0);
});

test('lots never round up past the risk budget', () => {
  const spec = { point: 0.1, minLots: 0.1, lotStep: 0.1, maxLots: 50, moneyPerPoint: 0.1 };
  const lots = orb.sizeForRisk(10000, 0.005, 44000, 43900, spec);
  const loss = 1000 * spec.moneyPerPoint * lots;
  assert.ok(loss <= 10000 * 0.005 + 1e-6, `risked ${loss}`);
});

test('a zero-distance stop sizes nothing rather than infinity', () => {
  const spec = { point: 0.1, minLots: 0.1, lotStep: 0.1, maxLots: 50, moneyPerPoint: 0.1 };
  assert.equal(orb.sizeForRisk(10000, 0.01, 44000, 44000, spec), 0);
});
