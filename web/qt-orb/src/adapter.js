// The platform seam: the only file that touches Liquid Charts / Figaro.
//
// READ THIS BEFORE FILLING IN THE FIGARO ADAPTER
// ==============================================
// Eight functions stand between the tested strategy and a live account. They
// are isolated here so that everything else -- the rules, the sizing, the
// session clock, the panel -- is verifiable without a broker, and so that
// wiring this platform up cannot accidentally change the strategy.
//
// The bodies of `figaroAdapter` are deliberately NOT guessed. Liquid Charts Pro
// is a FX Blue "Figaro" platform whose scripting API is documented at
// liquid-charts.gitbook.io, and that documentation could not be reached from
// the machine this was written on. Writing `SendOrder(...)` against an invented
// signature would produce code that looks finished, fails at runtime, and could
// fail *after* having sent a wrong order. A thrown "not wired up yet" is the
// correct behaviour until the real call is confirmed.
//
// What each function must do is fully specified below, so filling them in is
// mechanical once the docs are to hand. The exact doc pages needed:
//
//   1. Figaro framework -- how a script obtains the platform object, and
//      whether it runs in the page's own JS context (window.Liquid / similar)
//      or inside a sandboxed widget with a message bridge.
//   2. Market data -- the call that returns historical M1 bars for a symbol,
//      its argument order, and whether bar timestamps are UTC or server time.
//      (If they are server time, wrap them with clock.js before use: getting
//      this wrong shifts every opening range by hours.)
//   3. SendOrder -- the full parameter object, the OrderTypes enumeration, and
//      how stop-loss and take-profit are attached (as fields on the order, or
//      as a follow-up modify).
//   4. Positions / trade list -- how open positions are read, and which field
//      is the position id used for close and modify.
//   5. Account -- balance, equity and currency.
//   6. Symbol details -- the tick size / point, minimum and step lot size, and
//      money per point per lot. Without these, sizing is guesswork: a "point"
//      on US30 is not a point on EURUSD, and assuming otherwise is wrong by
//      orders of magnitude.
//
// Until then `mockAdapter` runs the whole bot end to end against a simulated
// account, which is how the loop in bot.js is tested.

'use strict';

const strategy = (typeof require !== 'undefined')
  ? require('./strategy.js')
  : globalThis.OrbStrategy;

const { BUY, SELL } = strategy;

// --------------------------------------------------------------------------
// the interface
// --------------------------------------------------------------------------
// An adapter is an object with these eight members. Everything is async so a
// message-bridge implementation is possible without changing any caller.
//
//   name                                  string, shown in the panel
//   async bars(symbol, minutes)           -> [{t,o,h,l,c,spread}]  M1, t = UTC ms, bar OPEN
//   async quote(symbol)                   -> {bid, ask}
//   async account()                       -> {balance, equity, currency}
//   async positions()                     -> [{id, symbol, direction, lots, entry, sl, tp, profit}]
//   async symbolSpec(symbol)              -> {point, minLots, lotStep, maxLots, moneyPerPoint}
//   async placeOrder(order)               -> {ok, id, reason}
//   async closePosition(id, reason)       -> {ok, reason}
//   async modifyPosition(id, {sl, tp})    -> {ok, reason}
//
// `direction` is +1 for a long and -1 for a short throughout, matching the
// Python side, so a sign convention cannot get lost in translation.

const REQUIRED = ['bars', 'quote', 'account', 'positions', 'symbolSpec',
  'placeOrder', 'closePosition', 'modifyPosition'];

// Checked at startup rather than on first use. A missing method that only
// surfaces when the first breakout fires is a missing method discovered at the
// worst possible moment.
function assertAdapter(adapter) {
  if (!adapter || typeof adapter !== 'object') throw new Error('no adapter supplied');
  const missing = REQUIRED.filter((m) => typeof adapter[m] !== 'function');
  if (missing.length) {
    throw new Error(`adapter '${adapter.name || 'unnamed'}' is missing: ${missing.join(', ')}`);
  }
  return adapter;
}

function notWired(what) {
  return () => {
    throw new Error(
      `${what} is not wired up to Liquid Charts yet. See the comment at the top of `
      + `adapter.js: this call needs the Figaro API documentation to be filled in, `
      + `and guessing its signature risks sending a malformed order.`);
  };
}

// --------------------------------------------------------------------------
// the Figaro adapter -- to be completed from the docs
// --------------------------------------------------------------------------
function figaroAdapter(platform) {
  // `platform` is whatever the Run Script widget exposes. Discover it with
  // discoverPlatform() below and paste the result into the issue/chat so the
  // remaining calls can be written against the real object.
  return {
    name: 'Liquid Charts (Figaro)',
    platform,
    bars: notWired('reading historical bars'),
    quote: notWired('reading the current quote'),
    account: notWired('reading the account'),
    positions: notWired('reading open positions'),
    symbolSpec: notWired('reading symbol details'),
    placeOrder: notWired('placing an order'),
    closePosition: notWired('closing a position'),
    modifyPosition: notWired('modifying a stop or target'),
  };
}

// A read-only probe of the page, to be run in the browser console on
// trader.liquidcharts.com. It touches nothing and sends nothing; it only
// reports which globals look like the platform, so the adapter above can be
// written against what is actually there rather than against a guess.
function discoverPlatform(root) {
  const scope = root || (typeof window !== 'undefined' ? window : {});
  const interesting = [];
  const wanted = /liquid|figaro|fxblue|chart|trade|order|account|broker|terminal|api/i;
  for (const key of Object.keys(scope)) {
    if (!wanted.test(key)) continue;
    let type;
    try {
      type = typeof scope[key];
    } catch (e) {
      type = 'unreadable';
    }
    if (type !== 'object' && type !== 'function') continue;
    let members = [];
    try {
      const value = scope[key];
      if (value) {
        members = Object.keys(value).concat(
          Object.getPrototypeOf(value) ? Object.getOwnPropertyNames(Object.getPrototypeOf(value)) : [],
        ).filter((m) => !m.startsWith('_')).slice(0, 60);
      }
    } catch (e) { /* some globals throw on enumeration */ }
    interesting.push({ key, type, members });
  }
  return interesting;
}

// --------------------------------------------------------------------------
// the mock adapter
// --------------------------------------------------------------------------
// A simulated account good enough to drive the whole loop: it fills at the
// quote, honours stops and targets against the bars it is fed, and refuses the
// things a real platform refuses (an unknown symbol, an off-grid lot size, a
// stop on the wrong side of the entry). A mock that accepts everything tests
// nothing that matters.
function mockAdapter(options) {
  const opts = options || {};
  const state = {
    equity: opts.equity === undefined ? 10000 : opts.equity,
    balance: opts.equity === undefined ? 10000 : opts.equity,
    currency: opts.currency || 'USD',
    barsBySymbol: opts.bars || {},
    specs: opts.specs || {},
    positions: [],
    placed: [],
    closed: [],
    modified: [],
    nextId: 1000,
    spread: opts.spread === undefined ? 10 : opts.spread,   // in points
    failPlace: opts.failPlace || null,
  };

  function spec(symbol) {
    return state.specs[symbol] || {
      point: 0.1, minLots: 0.01, lotStep: 0.01, maxLots: 100, moneyPerPoint: 0.1,
    };
  }

  function lastBar(symbol) {
    const list = state.barsBySymbol[symbol] || [];
    return list.length ? list[list.length - 1] : null;
  }

  const adapter = {
    name: 'Mock account',
    state,
    async bars(symbol) {
      const list = state.barsBySymbol[symbol];
      if (!list) throw new Error(`${symbol} is not available on this account`);
      return list;
    },
    async quote(symbol) {
      const bar = lastBar(symbol);
      const mid = bar ? bar.c : 0;
      const half = (state.spread * spec(symbol).point) / 2;
      return { bid: mid - half, ask: mid + half };
    },
    async account() {
      return { balance: state.balance, equity: state.equity, currency: state.currency };
    },
    async positions() {
      return state.positions.map((p) => Object.assign({}, p));
    },
    async symbolSpec(symbol) {
      return spec(symbol);
    },
    async placeOrder(order) {
      if (state.failPlace) return { ok: false, reason: state.failPlace };
      const s = spec(order.symbol);
      const steps = order.lots / s.lotStep;
      if (Math.abs(steps - Math.round(steps)) > 1e-6) {
        return { ok: false, reason: 'invalid volume' };
      }
      if (order.lots < s.minLots) return { ok: false, reason: 'volume below the minimum' };
      const q = await adapter.quote(order.symbol);
      const price = order.direction === BUY ? q.ask : q.bid;
      if ((order.direction === BUY && order.sl >= price)
        || (order.direction === SELL && order.sl <= price)) {
        return { ok: false, reason: 'the stop is on the wrong side of the entry' };
      }
      const id = state.nextId += 1;
      const position = {
        id, symbol: order.symbol, direction: order.direction, lots: order.lots,
        entry: price, sl: order.sl, tp: order.tp || 0, profit: 0,
      };
      state.positions.push(position);
      state.placed.push(Object.assign({}, order, { id, price }));
      return { ok: true, id, reason: '' };
    },
    async closePosition(id, reason) {
      const before = state.positions.length;
      state.positions = state.positions.filter((p) => p.id !== id);
      state.closed.push({ id, reason });
      return { ok: state.positions.length < before, reason: '' };
    },
    async modifyPosition(id, changes) {
      const position = state.positions.find((p) => p.id === id);
      if (!position) return { ok: false, reason: 'no such position' };
      if (changes.sl !== undefined) position.sl = changes.sl;
      if (changes.tp !== undefined) position.tp = changes.tp;
      state.modified.push({ id, changes });
      return { ok: true, reason: '' };
    },
  };
  return adapter;
}

const api = { REQUIRED, assertAdapter, figaroAdapter, discoverPlatform, mockAdapter };

if (typeof module !== 'undefined' && module.exports) module.exports = api;
if (typeof globalThis !== 'undefined') globalThis.OrbAdapter = api;
