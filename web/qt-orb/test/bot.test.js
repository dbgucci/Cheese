// The loop, driven end to end against the mock account.
//
// The clock is injected, so a whole trading session runs in milliseconds and the
// awkward moments -- the minute before the open, the minute after the close, a
// halted day -- are reachable instead of waiting for 14:30 on a Tuesday.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const clock = require('../src/clock.js');
const strategy = require('../src/strategy.js');
const adapters = require('../src/adapter.js');
const { Bot } = require('../src/bot.js');

const US = clock.SESSIONS.us_cash;
const DAY = { year: 2026, month: 3, day: 2 };
const OPEN = Date.UTC(2026, 2, 2, 14, 30);
const SYMBOL = 'US30';
const SPEC = { point: 0.1, minLots: 0.1, lotStep: 0.1, maxLots: 50, moneyPerPoint: 0.1 };

function bars(rows) {
  return rows.map((r, i) => ({
    t: OPEN + i * 60000, o: r[2], h: r[0], l: r[1], c: r[2], spread: 10,
  }));
}

// 15 flat range bars (200 broker points wide), then whatever follows.
function rangeThen(rows) {
  const head = [];
  for (let i = 0; i < 15; i += 1) head.push([44010, 43990, 44000]);
  return bars(head.concat(rows || []));
}

function makeBot(opts) {
  const o = opts || {};
  const adapter = adapters.mockAdapter({
    equity: o.equity === undefined ? 50000 : o.equity,
    bars: { [SYMBOL]: o.bars || rangeThen([[44020, 44005, 44015]]) },
    specs: { [SYMBOL]: SPEC },
    failPlace: o.failPlace || null,
  });
  const events = [];
  const bot = new Bot({
    adapter,
    symbols: [SYMBOL],
    strategy: Object.assign({ minRangeAdrFraction: 0, maxRangeAdrFraction: 10 }, o.strategy),
    limits: Object.assign({ dryRun: false }, o.limits),
    onEvent: (e) => events.push(e),
    now: () => o.now === undefined ? OPEN + 16 * 60000 + 5000 : o.now,
    confirm: o.confirm,
  });
  return { bot, adapter, events };
}

function kinds(events) { return events.map((e) => e.kind); }

// ------------------------------ opening ------------------------------
test('a breakout after the range is traded', async () => {
  const { bot, adapter, events } = makeBot();
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 1);
  const order = adapter.state.placed[0];
  assert.equal(order.direction, strategy.BUY);
  assert.equal(order.sl, 43990);
  assert.ok(kinds(events).includes('opened'));
});

test('nothing is traded while the range is still forming', async () => {
  const { bot, adapter } = makeBot({ now: OPEN + 8 * 60000 });
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 0);
  assert.equal(bot.status.get(SYMBOL), 'range forming');
});

test('nothing is traded before the open', async () => {
  const { bot, adapter } = makeBot({ now: OPEN - 60000 });
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 0);
  assert.equal(bot.status.get(SYMBOL), 'before the open');
});

test('the bar still in progress is not treated as closed', async () => {
  // The 14:45 bar exists but the clock is inside it, so its close does not
  // exist yet and using it would trade a signal no backtest ever saw.
  const { bot, adapter } = makeBot({ now: OPEN + 15 * 60000 + 30000 });
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 0);
});

test('the position is sized so the stop costs the risk budget', async () => {
  const { bot, adapter } = makeBot({ equity: 50000, limits: { riskFraction: 0.005 } });
  await bot.cycle();
  const order = adapter.state.placed[0];
  const stopPoints = Math.abs(order.price - order.sl) / SPEC.point;
  const loss = stopPoints * SPEC.moneyPerPoint * order.lots;
  assert.ok(loss <= 50000 * 0.005 + 1e-6, `risked ${loss}`);
  assert.ok(loss > 50000 * 0.005 * 0.7, `sized far too small: ${loss}`);
});

test('a second position on the same instrument is not opened', async () => {
  const { bot, adapter } = makeBot();
  await bot.cycle();
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 1);
});

test('the session is over for the day after its one trade', async () => {
  const { bot, adapter } = makeBot();
  await bot.cycle();
  adapter.state.positions = [];            // the stop filled
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 1);
  assert.equal(bot.status.get(SYMBOL), 'done for today');
});

test('a range that fails its filters is refused once, with numbers', async () => {
  const head = [];
  for (let i = 0; i < 15; i += 1) head.push([44000.5, 44000, 44000]);
  const { bot, adapter, events } = makeBot({
    bars: bars(head.concat([[44002, 44000, 44001]])),
    strategy: { minRangeAdrFraction: 0, maxRangeAdrFraction: 10 },
  });
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 0);
  const skipped = events.filter((e) => e.kind === 'skipped');
  assert.ok(skipped.length >= 1);
  assert.match(skipped[0].detail, /round-trip cost/);
  const before = events.length;
  await bot.cycle();
  assert.equal(events.length, before, 'the same refusal was logged twice');
});

// ------------------------------ dry run ------------------------------
test('a dry run reaches no platform at all', async () => {
  const { bot, adapter, events } = makeBot({ limits: { dryRun: true } });
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 0);
  const opened = events.find((e) => e.kind === 'opened');
  assert.match(opened.detail, /dry run/);
});

test('dry run is the default', () => {
  const { DEFAULT_LIMITS } = require('../src/bot.js');
  assert.equal(DEFAULT_LIMITS.dryRun, true);
});

// ------------------------------ confirmation ------------------------------
test('confirm-every-order can decline a trade', async () => {
  const asked = [];
  const { bot, adapter } = makeBot({
    limits: { dryRun: false, confirmEveryOrder: true },
    confirm: async (order) => { asked.push(order); return false; },
  });
  await bot.cycle();
  assert.equal(asked.length, 1);
  assert.equal(adapter.state.placed.length, 0);
});

test('confirm-every-order can accept a trade', async () => {
  const { bot, adapter } = makeBot({
    limits: { dryRun: false, confirmEveryOrder: true },
    confirm: async () => true,
  });
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 1);
});

// ------------------------------ management ------------------------------
test('the stop moves to entry once the trade is 1R ahead', async () => {
  const { bot, adapter } = makeBot();
  await bot.cycle();
  const position = adapter.state.positions[0];
  const risk = Math.abs(position.entry - position.sl);
  // Push the last bar's close past 1R so the mock quote follows it.
  const list = adapter.state.barsBySymbol[SYMBOL];
  list.push({ t: OPEN + 17 * 60000, o: 0, h: 0, l: 0,
    c: position.entry + risk * 1.2, spread: 10 });
  await bot.cycle();
  assert.equal(adapter.state.modified.length, 1);
  assert.equal(adapter.state.modified[0].changes.sl, position.entry);
});

test('the stop does not move before 1R', async () => {
  const { bot, adapter } = makeBot();
  await bot.cycle();
  await bot.cycle();
  assert.equal(adapter.state.modified.length, 0);
});

test('the stop is only moved once', async () => {
  const { bot, adapter } = makeBot();
  await bot.cycle();
  const position = adapter.state.positions[0];
  const risk = Math.abs(position.entry - position.sl);
  adapter.state.barsBySymbol[SYMBOL].push({ t: OPEN + 17 * 60000, o: 0, h: 0, l: 0,
    c: position.entry + risk * 1.2, spread: 10 });
  await bot.cycle();
  await bot.cycle();
  assert.equal(adapter.state.modified.length, 1);
});

test('a position is flat before the session closes', async () => {
  const flatMoment = clock.sessionClose(US, DAY).getTime() - 5 * 60000;
  const { bot, adapter, events } = makeBot();
  await bot.cycle();
  assert.equal(adapter.state.positions.length, 1);
  bot.now = () => flatMoment;
  await bot.cycle();
  assert.equal(adapter.state.closed.length, 1);
  assert.ok(events.some((e) => e.kind === 'closed' && /close/.test(e.detail)));
});

test('a position is flat on a weekend', async () => {
  const { bot, adapter } = makeBot();
  await bot.cycle();
  bot.now = () => Date.UTC(2026, 2, 7, 15, 0);      // Saturday
  await bot.cycle();
  assert.equal(adapter.state.closed.length, 1);
});

// ------------------------------ the breakers ------------------------------
test('a closed position feeds the consecutive-loss counter', async () => {
  const { bot, adapter } = makeBot();
  await bot.cycle();
  adapter.state.positions[0].profit = -120;
  await bot.cycle();                                 // sees the floating loss
  adapter.state.positions = [];                      // then it closes
  await bot.cycle();
  assert.equal(bot.day.consecutiveLosses, 1);
  assert.equal(bot.stats.total, 1);
  assert.equal(bot.stats.losses, 1);
});

test('four losses in a row halt the day', async () => {
  const { bot, events } = makeBot();
  bot.ensureDay(50000);
  bot.day.consecutiveLosses = 4;
  const allowed = bot.mayOpen(50000, 0);
  assert.equal(allowed.ok, false);
  assert.match(allowed.reason, /losses in a row/);
  assert.ok(events.some((e) => e.kind === 'halted'));
});

test('the daily loss limit halts the day and says by how much', async () => {
  const { bot } = makeBot();
  bot.ensureDay(50000);
  const allowed = bot.mayOpen(50000 * 0.96, 0);      // down 4%, limit 3%
  assert.equal(allowed.ok, false);
  assert.match(allowed.reason, /down 4\.0% today/);
  assert.equal(bot.day.halted, true);
});

test('a halted day blocks entries', async () => {
  const { bot, adapter, events } = makeBot();
  bot.ensureDay(50000);
  bot.halt('testing');
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 0);
  assert.ok(events.some((e) => e.kind === 'blocked'));
});

test('the trades-per-day cap is enforced', async () => {
  const { bot } = makeBot({ limits: { dryRun: false, maxTradesPerDay: 1 } });
  bot.ensureDay(50000);
  bot.day.trades = 1;
  assert.equal(bot.mayOpen(50000, 0).ok, false);
});

test('the open-positions cap is enforced', async () => {
  const { bot } = makeBot({ limits: { dryRun: false, maxOpenPositions: 1 } });
  bot.ensureDay(50000);
  assert.equal(bot.mayOpen(50000, 1).ok, false);
});

// ------------------------------ failure handling ------------------------------
test('a rejected order is reported rather than swallowed', async () => {
  const { bot, events } = makeBot({ failPlace: 'market closed' });
  await bot.cycle();
  const errors = events.filter((e) => e.kind === 'error');
  assert.ok(errors.some((e) => /market closed/.test(e.detail)));
});

test('an unreadable account stops the cycle rather than trading blind', async () => {
  const { bot, adapter, events } = makeBot();
  adapter.account = async () => { throw new Error('not logged in'); };
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 0);
  assert.ok(events.some((e) => e.kind === 'error' && /not logged in/.test(e.detail)));
});

test('an unmapped symbol is refused rather than guessed at', async () => {
  const adapter = adapters.mockAdapter({ bars: { BTCUSD: rangeThen([]) } });
  const events = [];
  const bot = new Bot({ adapter, symbols: ['BTCUSD'], onEvent: (e) => events.push(e),
    now: () => OPEN + 16 * 60000 });
  await bot.cycle();
  assert.ok(events.some((e) => /no session mapped/.test(e.detail)));
  assert.equal(bot.status.get('BTCUSD'), 'not traded');
});

test('a contradictory configuration refuses to start', () => {
  assert.throws(() => new Bot({
    adapter: adapters.mockAdapter({}),
    symbols: [SYMBOL],
    strategy: { minRangeAdrFraction: 0.9, maxRangeAdrFraction: 0.1 },
  }), /inverted/);
});

test('an adapter missing a method is caught at construction', () => {
  const broken = adapters.mockAdapter({});
  delete broken.placeOrder;
  assert.throws(() => new Bot({ adapter: broken, symbols: [SYMBOL] }), /missing: placeOrder/);
});

test('the Figaro adapter refuses to guess its own API', () => {
  const adapter = adapters.figaroAdapter({});
  assert.throws(() => adapter.placeOrder({}), /not wired up/);
});

// ------------------------------ reporting ------------------------------
test('session stats count only what this bot managed', async () => {
  const { bot, adapter } = makeBot();
  await bot.cycle();
  adapter.state.positions[0].profit = 93.95;
  await bot.cycle();
  adapter.state.positions = [];
  await bot.cycle();
  const stats = bot.stats;
  assert.equal(stats.total, 1);
  assert.equal(stats.wins, 1);
  assert.equal(stats.winRate, 1);
  assert.equal(stats.net.toFixed(2), '93.95');
  assert.equal(stats.best.toFixed(2), '93.95');
});

test('empty stats do not divide by zero', () => {
  const { bot } = makeBot();
  assert.deepEqual(bot.stats, { total: 0, wins: 0, losses: 0, winRate: 0, net: 0, best: 0, worst: 0 });
});

test('the CSV has a header and one row per closed trade', async () => {
  const { bot, adapter } = makeBot();
  await bot.cycle();
  adapter.state.positions[0].profit = 12.5;
  await bot.cycle();
  adapter.state.positions = [];
  await bot.cycle();
  const lines = bot.toCsv().split('\n');
  assert.match(lines[0], /^time_utc,symbol,side,lots,entry,profit,reason$/);
  assert.equal(lines.length, 2);
  assert.match(lines[1], /US30,BUY/);
});
