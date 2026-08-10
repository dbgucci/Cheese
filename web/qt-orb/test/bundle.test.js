// The built bundle, executed in a browser-like DOM.
//
// The unit tests cover the modules. This covers the thing they cannot: that the
// concatenated, `require`-shimmed, IIFE-wrapped artefact somebody is about to
// paste into a live trading page actually runs, mounts, and drives a bot.
//
// It caught a real one already: every source declares `const api` at the end,
// so the first version of the bundle was a SyntaxError the moment it was pasted
// -- invisible to `node --test`, which never loads the bundle at all.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (err) {
  JSDOM = null;
}

const BUNDLE = path.join(__dirname, '..', 'dist', 'orb-overlay.js');

function freshBundle() {
  const { build } = require('../build.js');
  build();
  return fs.readFileSync(BUNDLE, 'utf8');
}

// jsdom windows must be closed. `pretendToBeVisual` runs an animation loop and
// the bundle installs a poll interval once started, and either one keeps node's
// event loop alive -- a hung test suite rather than a failing one.
async function withDom(body) {
  const dom = new JSDOM('<!doctype html><html><body></body></html>',
    { runScripts: 'outside-only', pretendToBeVisual: true });
  try {
    // Awaited, not returned: a bare `return body(dom)` hands back the promise
    // and lets `finally` tear the window down before the body has run, so every
    // assertion after the first await sees an already-closed document.
    return await body(dom);
  } finally {
    try {
      if (dom.window.ORB && dom.window.ORB.panel) dom.window.ORB.panel.onStop();
    } catch (e) { /* never started */ }
    dom.window.close();
  }
}

test('the bundle is syntactically valid', () => {
  // eslint-disable-next-line no-new-func
  new Function(freshBundle());
});

test('the bundle carries no CommonJS leftovers', () => {
  const src = freshBundle();
  assert.equal(/module\.exports/.test(src), false);
  assert.equal(/^\s*require\(/m.test(src.replace(/function require/g, '')), false);
});

test('the bundle mounts a panel and exposes handles', { skip: !JSDOM }, () => withDom((dom) => {
  dom.window.eval(freshBundle());

  const panel = dom.window.document.getElementById('orb-autobot-panel');
  assert.ok(panel, 'the panel did not mount');
  assert.ok(dom.window.ORB, 'window.ORB was not published');
  assert.ok(dom.window.ORB.clock && dom.window.ORB.strategy);

  // Dry run must be the state it lands in, not a state the user has to find.
  assert.equal(dom.window.ORB.panel.dryRun.checked, true);
  assert.match(panel.textContent, /dry run/i);
  assert.match(panel.textContent, /ORB Autobot/);
}));

test('with no adapter installed the panel refuses to start', { skip: !JSDOM }, () => withDom((dom) => {
  dom.window.eval(freshBundle());
  // The one thing that must not happen is a bot that "runs" against a guessed
  // API and reports success while sending nothing, or worse, something wrong.
  assert.equal(dom.window.ORB.panel.startBtn.disabled, true);
  const log = dom.window.document.querySelector('#orb-autobot-panel .orb-log');
  assert.match(log.textContent, /cannot reach your account/);
}));

test('an installed adapter drives a full session in the page', { skip: !JSDOM }, () => withDom(async (dom) => {
  // Build the mock account before the bundle loads, the same way a real Figaro
  // adapter would be installed: window.ORB_ADAPTER.
  const clock = require('../src/clock.js');
  const adapters = require('../src/adapter.js');
  const DAY = { year: 2026, month: 3, day: 2 };
  const OPEN = clock.sessionOpen(clock.SESSIONS.us_cash, DAY).getTime();
  const rows = [];
  for (let i = 0; i < 15; i += 1) rows.push([44010, 43990, 44000]);
  rows.push([44020, 44005, 44015]);
  const bars = rows.map((r, i) => ({
    t: OPEN + i * 60000, o: r[2], h: r[0], l: r[1], c: r[2], spread: 10,
  }));
  const adapter = adapters.mockAdapter({
    equity: 50000,
    bars: { US30: bars },
    specs: { US30: { point: 0.1, minLots: 0.1, lotStep: 0.1, maxLots: 50, moneyPerPoint: 0.1 } },
  });
  dom.window.ORB_ADAPTER = adapter;
  dom.window.eval(freshBundle());

  const { panel } = dom.window.ORB;
  panel.symbolsInput.value = 'US30';
  panel.dryRun.checked = false;             // the mock account, not a real one
  // "Confirm every order" defaults to ON, which is the right default and the
  // reason this line is needed: jsdom implements no window.confirm, so it
  // returns undefined, the bot reads that as "declined", and no order is sent.
  // Exactly what should happen -- but it is not what this test is measuring.
  panel.confirmEach.checked = false;
  assert.equal(panel.startBtn.disabled, false);

  panel.onStart(panel.settings());
  const bot = dom.window.ORB.bot;
  assert.ok(bot, 'no bot was constructed');

  // onStart fires a cycle immediately, against the real wall clock. Wait for it
  // to finish before driving one at the simulated session time: the bot refuses
  // to re-enter a cycle that is still in flight, which is the point of the
  // guard and would otherwise make this test silently do nothing.
  for (let i = 0; i < 200 && bot._inFlight; i += 1) {
    await new Promise((r) => setTimeout(r, 5));
  }
  assert.equal(bot._inFlight, false, 'the first cycle never finished');

  bot.now = () => OPEN + 16 * 60000 + 5000;
  await bot.cycle();
  assert.equal(adapter.state.placed.length, 1, 'no order reached the mock account');
  assert.equal(adapter.state.placed[0].direction, 1);

  panel.setStats(bot.stats, 'USD');
  panel.setSessions([{ symbol: 'US30', window: '14:30-14:45', status: 'in a trade', detail: '' }]);
  const text = dom.window.document.getElementById('orb-autobot-panel').textContent;
  assert.match(text, /US30/);
  assert.match(text, /in a trade/);
}));

test('the panel can be removed cleanly', { skip: !JSDOM }, () => withDom((dom) => {
  dom.window.eval(freshBundle());
  dom.window.ORB.panel.destroy();
  assert.equal(dom.window.document.getElementById('orb-autobot-panel'), null);
}));

test('mounting twice does not stack panels', { skip: !JSDOM }, () => withDom((dom) => {
  dom.window.eval(freshBundle());
  dom.window.ORB.panel.mount();
  const all = dom.window.document.querySelectorAll('#orb-autobot-panel');
  assert.equal(all.length, 1);
}));
