// The DST arithmetic, which is the part most likely to be silently wrong.
//
// These assertions mirror tests/test_markets_clock.py deliberately: if the
// JavaScript and the Python disagree about when a market opens, the strategy
// running in the browser is not the strategy that was backtested.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const clock = require('../src/clock.js');

const US = clock.SESSIONS.us_cash;
const LON = clock.SESSIONS.london;

function iso(d) { return d.toISOString().slice(0, 16) + 'Z'; }

test('the New York open is 14:30 UTC in winter', () => {
  const open = clock.sessionOpen(US, { year: 2026, month: 1, day: 14 });
  assert.equal(iso(open), '2026-01-14T14:30Z');
});

test('the New York open is 13:30 UTC in summer', () => {
  const open = clock.sessionOpen(US, { year: 2026, month: 7, day: 14 });
  assert.equal(iso(open), '2026-07-14T13:30Z');
});

test('the open shifts across the DST boundary itself', () => {
  // US clocks moved on 8 March 2026.
  assert.equal(clock.sessionOpen(US, { year: 2026, month: 3, day: 6 }).getUTCHours(), 14);
  assert.equal(clock.sessionOpen(US, { year: 2026, month: 3, day: 9 }).getUTCHours(), 13);
});

test('the day after a transition is not an hour out', () => {
  // The bug the two-pass offset resolution exists to prevent: a single-pass
  // conversion picks the offset in force *before* the transition.
  assert.equal(iso(clock.sessionOpen(US, { year: 2026, month: 3, day: 8 })), '2026-03-08T13:30Z');
  assert.equal(iso(clock.sessionOpen(LON, { year: 2026, month: 3, day: 30 })), '2026-03-30T07:00Z');
});

test('London and New York are not always five hours apart', () => {
  // Europe changed on 29 March 2026, the US on 8 March. In between the gap is
  // four hours, which breaks a bot that keeps two hardcoded UTC tables.
  const gap = (y, m, d) => {
    const ny = clock.sessionOpen(US, { year: y, month: m, day: d });
    const lon = clock.sessionOpen(LON, { year: y, month: m, day: d });
    // 09:30 NY against 08:00 London: 1.5h of nominal difference removed.
    return (ny - lon) / 3600000 - 1.5;
  };
  assert.equal(gap(2026, 3, 2), 5);
  assert.equal(gap(2026, 3, 16), 4);
  assert.equal(gap(2026, 4, 6), 5);
});

test('the session close is DST-correct too', () => {
  assert.equal(clock.sessionClose(US, { year: 2026, month: 1, day: 14 }).getUTCHours(), 21);
  assert.equal(clock.sessionClose(US, { year: 2026, month: 7, day: 14 }).getUTCHours(), 20);
});

test('a session date is the exchange calendar, not UTC', () => {
  const late = new Date('2026-01-15T00:30:00Z');   // 19:30 Wed 14th in New York
  assert.deepEqual(clock.sessionDate(US, late), { year: 2026, month: 1, day: 14 });
});

test('weekends are not trading days', () => {
  assert.equal(clock.isWeekday({ year: 2026, month: 3, day: 6 }), true);
  assert.equal(clock.isWeekday({ year: 2026, month: 3, day: 7 }), false);
  assert.equal(clock.isWeekday({ year: 2026, month: 3, day: 8 }), false);
});

test('broker suffixes do not hide the instrument', () => {
  // XAUUSD247 is the symbol in the recording this was built against.
  const cases = [
    ['XAUUSD247', 'XAUUSD'], ['XAUUSD.r', 'XAUUSD'], ['XAUUSD-ECN', 'XAUUSD'],
    ['US30cash', 'US30'], ['NAS100_m', 'NAS100'], ['SPX500.pro', 'SPX500'],
    ['EURUSD.a', 'EURUSD'],
  ];
  for (const [decorated, expected] of cases) {
    assert.equal(clock.baseName(decorated), expected, decorated);
  }
});

test('indices map to their cash open and metals to London', () => {
  assert.equal(clock.sessionFor('NAS100_m').key, 'us_cash');
  assert.equal(clock.sessionFor('XAUUSD247').key, 'london');
  assert.equal(clock.sessionFor('EURUSD').key, 'london');
});

test('an unmapped instrument throws rather than guessing', () => {
  assert.throws(() => clock.sessionFor('BTCUSD'), /no session mapped/);
});

test('an explicit session override wins', () => {
  assert.equal(clock.sessionFor('XAUUSD', 'comex').key, 'comex');
});

test('indices get fifteen minutes and metals thirty', () => {
  assert.equal(clock.suggestRangeMinutes('NAS100'), 15);
  assert.equal(clock.suggestRangeMinutes('US30cash'), 15);
  assert.equal(clock.suggestRangeMinutes('XAUUSD247'), 30);
  assert.equal(clock.suggestRangeMinutes('EURUSD'), 30);
});

test('the offset is measured, not assumed', () => {
  assert.equal(clock.offsetMinutes(new Date('2026-01-14T15:00:00Z'), 'America/New_York'), -300);
  assert.equal(clock.offsetMinutes(new Date('2026-07-14T15:00:00Z'), 'America/New_York'), -240);
  assert.equal(clock.offsetMinutes(new Date('2026-01-14T15:00:00Z'), 'UTC'), 0);
});
