// The loop: strategy plus adapter, with the limits that stop it.
//
// The order of operations in a cycle is not arbitrary, and matches autobot.py:
//
//   1. Read positions from the platform. Never from memory -- a position closed
//      by its stop, by hand, or by a page reload is one this loop must not
//      believe in.
//   2. Manage what exists before looking for anything new. A loop that opens a
//      trade in the cycle it should have flattened one has its priorities
//      backwards, and the flatten is the part that protects the account.
//   3. Only then look for an entry.
//
// Everything the loop does or refuses to do is recorded with a reason, because
// "it isn't trading" with no explanation is indistinguishable from a bot that
// has been broken since Tuesday.

'use strict';

const clock = (typeof require !== 'undefined') ? require('./clock.js') : globalThis.OrbClock;
const strategy = (typeof require !== 'undefined') ? require('./strategy.js') : globalThis.OrbStrategy;
const adapters = (typeof require !== 'undefined') ? require('./adapter.js') : globalThis.OrbAdapter;

const { BUY, SELL } = strategy;

const DEFAULT_LIMITS = {
  riskFraction: 0.005,          // of equity, per trade
  maxDailyLossFraction: 0.03,   // stop for the day at -3% of the day's start equity
  maxOpenPositions: 3,
  maxTradesPerDay: 6,
  consecutiveLossesHalt: 4,
  confirmEveryOrder: false,     // ask before each order; the safest way to test live
  dryRun: true,                 // nothing reaches the platform until this is off
};

class Bot {
  constructor(options) {
    const o = options || {};
    this.adapter = adapters.assertAdapter(o.adapter);
    this.symbols = (o.symbols || []).slice();
    this.cfg = strategy.config(o.strategy);
    this.limits = Object.assign({}, DEFAULT_LIMITS, o.limits);
    this.onEvent = o.onEvent || (() => {});
    this.confirm = o.confirm || (async () => true);
    this.now = o.now || (() => Date.now());

    const problems = strategy.validate(this.cfg);
    if (problems.length) throw new Error(`contradictory settings: ${problems.join('; ')}`);

    this.running = false;
    this.day = null;              // { key, startEquity, trades, consecutiveLosses, halted, haltReason }
    this.trades = [];             // this session's completed trades, for the panel and CSV
    this.ranges = new Map();      // `${symbol}|${dayKey}` -> range
    this.entries = new Map();     // ...            -> count
    this.barred = new Map();      // ...            -> array of directions
    this.movedToBreakeven = new Set();
    this.watching = new Map();    // position id -> last seen position
    this.lastSaid = new Map();    // symbol -> last reason, to keep the log readable
    this.status = new Map();      // symbol -> a short state for the panel
    this._inFlight = false;
  }

  say(symbol, kind, detail, once) {
    if (once && this.lastSaid.get(symbol) === detail) return;
    this.lastSaid.set(symbol, detail);
    this.onEvent({ at: this.now(), symbol, kind, detail });
  }

  // ------------------------------------------------------------- day state
  ensureDay(equity) {
    const key = new Date(this.now()).toISOString().slice(0, 10);
    if (!this.day || this.day.key !== key) {
      this.day = {
        key, startEquity: equity, trades: 0, consecutiveLosses: 0,
        halted: false, haltReason: '',
      };
    }
    return this.day;
  }

  halt(reason) {
    if (this.day) {
      this.day.halted = true;
      this.day.haltReason = reason;
    }
    this.say('-', 'halted', reason, true);
  }

  // The circuit breakers. None of these can be overridden by a signal, however
  // convincing -- which is the point, because a strategy whose regime has ended
  // looks exactly like a strategy that is working, from the inside.
  mayOpen(equity, openPositions) {
    const day = this.ensureDay(equity);
    if (day.halted) return { ok: false, reason: `halted for the day: ${day.haltReason}` };

    const drawdown = day.startEquity ? (day.startEquity - equity) / day.startEquity : 0;
    if (drawdown >= this.limits.maxDailyLossFraction) {
      this.halt(`down ${(drawdown * 100).toFixed(1)}% today, the limit is `
        + `${(this.limits.maxDailyLossFraction * 100).toFixed(0)}%`);
      return { ok: false, reason: day.haltReason };
    }
    if (day.consecutiveLosses >= this.limits.consecutiveLossesHalt) {
      this.halt(`${day.consecutiveLosses} losses in a row`);
      return { ok: false, reason: day.haltReason };
    }
    if (day.trades >= this.limits.maxTradesPerDay) {
      return { ok: false, reason: `${day.trades} trades already today, the limit is `
        + `${this.limits.maxTradesPerDay}` };
    }
    if (openPositions >= this.limits.maxOpenPositions) {
      return { ok: false, reason: `${openPositions} positions open, the limit is `
        + `${this.limits.maxOpenPositions}` };
    }
    return { ok: true, reason: '' };
  }

  // -------------------------------------------------------------- one cycle
  //
  // Guarded against re-entry, which is not a theoretical concern. Every step of
  // a cycle awaits the platform, and the caller polls on a timer: if a cycle
  // takes longer than the poll interval -- one slow history call is enough --
  // a second cycle starts, sees no open position because the first has not
  // placed one yet, and places a second order for the same signal. Two
  // positions, double the risk, from one breakout. Found by a test that ran two
  // cycles concurrently and got two fills.
  async cycle() {
    if (this._inFlight) return;
    this._inFlight = true;
    try {
      await this._cycle();
    } finally {
      this._inFlight = false;
    }
  }

  async _cycle() {
    let account;
    try {
      account = await this.adapter.account();
    } catch (err) {
      this.say('-', 'error', `cannot read the account: ${err.message}`, true);
      return;
    }
    this.ensureDay(account.equity);

    let positions;
    try {
      positions = await this.adapter.positions();
    } catch (err) {
      this.say('-', 'error', `cannot read positions: ${err.message}`, true);
      return;
    }

    this.settle(positions);
    const mine = positions.filter((p) => this.symbols.includes(p.symbol));

    for (const position of mine) {
      try {
        await this.manage(position);
      } catch (err) {
        this.say(position.symbol, 'error', `managing: ${err.message}`, true);
      }
    }

    const held = new Set((await this.adapter.positions()).map((p) => p.symbol));
    for (const symbol of this.symbols) {
      if (held.has(symbol)) continue;
      try {
        await this.seekEntry(symbol, account.equity, held.size);
      } catch (err) {
        this.say(symbol, 'error', `looking for an entry: ${err.message}`, true);
      }
    }
  }

  // Positions that have vanished are fed back into the breakers.
  //
  // Without this the consecutive-loss halt sits at zero forever: safety rails
  // configured and not connected, which is worse than none at all because the
  // panel reports that they are there.
  settle(positions) {
    const live = new Map(positions.map((p) => [p.id, p]));
    for (const [id, lastSeen] of Array.from(this.watching.entries())) {
      if (live.has(id)) continue;
      const profit = Number(lastSeen.profit) || 0;
      if (profit < 0) this.day.consecutiveLosses += 1;
      else this.day.consecutiveLosses = 0;
      this.trades.push({
        at: this.now(), symbol: lastSeen.symbol,
        direction: lastSeen.direction, lots: lastSeen.lots,
        entry: lastSeen.entry, exit: null, profit,
        reason: lastSeen.orbReason || '',
      });
      this.movedToBreakeven.delete(id);
      this.watching.delete(id);
      this.say(lastSeen.symbol, 'settled',
        `position ${id} closed at ${profit >= 0 ? '+' : ''}${profit.toFixed(2)}`);
    }
    for (const [id, p] of live) {
      const previous = this.watching.get(id);
      if (previous && previous.orbReason) p.orbReason = previous.orbReason;
      this.watching.set(id, p);
    }
  }

  // ----------------------------------------------------------------- manage
  async manage(position) {
    const symbol = position.symbol;
    const spec = clock.sessionFor(symbol);
    const cfg = this.cfgFor(symbol);
    const localDate = clock.sessionDate(spec, new Date(this.now()));
    const flatAt = clock.sessionClose(spec, localDate).getTime()
      - cfg.flatBeforeCloseMinutes * 60000;

    if (this.now() >= flatAt || !clock.isWeekday(localDate) || this.day.halted) {
      const why = this.day.halted ? `halted: ${this.day.haltReason}`
        : !clock.isWeekday(localDate) ? 'weekend'
          : `${spec.label} close at ${clock.hhmm(new Date(flatAt + cfg.flatBeforeCloseMinutes * 60000))} UTC`;
      if (this.limits.dryRun) {
        this.say(symbol, 'closed', `dry run: would close ${position.id} (${why})`);
      } else {
        const result = await this.adapter.closePosition(position.id, why);
        this.say(symbol, result.ok ? 'closed' : 'error',
          `${why}: ${result.ok ? 'closed' : result.reason}`);
      }
      return;
    }

    // Breakeven. Without the range that produced the trade the risk distance is
    // unknown, so the stop is left alone rather than moved to a guess.
    if (cfg.breakevenAtR === null || this.movedToBreakeven.has(position.id)) return;
    const risk = Math.abs(position.entry - position.sl);
    if (!(risk > 0)) return;
    const trigger = position.entry + cfg.breakevenAtR * risk * (position.direction === BUY ? 1 : -1);
    const quote = await this.adapter.quote(symbol);
    const price = position.direction === BUY ? quote.bid : quote.ask;
    const reached = position.direction === BUY ? price >= trigger : price <= trigger;
    if (!reached) return;

    // A trailing stop may only ever move in the protective direction: a bug
    // that loosens it turns a bounded loss into an unbounded one.
    if (position.direction === BUY && position.entry <= position.sl) return;
    if (position.direction === SELL && position.entry >= position.sl) return;

    if (this.limits.dryRun) {
      this.movedToBreakeven.add(position.id);
      this.say(symbol, 'trailed', `dry run: would move the stop to entry ${position.entry}`);
      return;
    }
    const result = await this.adapter.modifyPosition(position.id, { sl: position.entry });
    if (result.ok) {
      this.movedToBreakeven.add(position.id);
      this.say(symbol, 'trailed', `${cfg.breakevenAtR}R ahead: stop moved to entry`);
    } else {
      this.say(symbol, 'skipped', `breakeven: ${result.reason}`, true);
    }
  }

  cfgFor(symbol) {
    return Object.assign({}, this.cfg, {
      rangeMinutes: this.cfg.rangeMinutesOverride || clock.suggestRangeMinutes(symbol),
    });
  }

  // ------------------------------------------------------------------ entry
  async seekEntry(symbol, equity, openPositions) {
    let spec;
    try {
      spec = clock.sessionFor(symbol);
    } catch (err) {
      this.status.set(symbol, 'not traded');
      this.say(symbol, 'skipped', err.message, true);
      return;
    }
    const cfg = this.cfgFor(symbol);
    const localDate = clock.sessionDate(spec, new Date(this.now()));
    const key = `${symbol}|${clock.dateKey(localDate)}`;

    if (!clock.isWeekday(localDate)) {
      this.status.set(symbol, 'closed');
      return;
    }
    if ((this.entries.get(key) || 0) >= cfg.maxTradesPerSession) {
      this.status.set(symbol, 'done for today');
      return;
    }

    const open = clock.sessionOpen(spec, localDate).getTime();
    const rangeEnd = open + cfg.rangeMinutes * 60000;
    if (this.now() < open) { this.status.set(symbol, 'before the open'); return; }
    if (this.now() < rangeEnd) { this.status.set(symbol, 'range forming'); return; }

    const allowed = this.mayOpen(equity, openPositions);
    if (!allowed.ok) {
      this.status.set(symbol, 'blocked');
      this.say(symbol, 'blocked', allowed.reason, true);
      return;
    }

    const symbolSpec = await this.adapter.symbolSpec(symbol);
    const bars = await this.adapter.bars(symbol, cfg.adrDays * 24 * 60);
    if (!bars || !bars.length) {
      this.say(symbol, 'skipped', 'no bars came back for this session', true);
      return;
    }

    const rng = strategy.buildRange(bars, symbol, spec, localDate, cfg, symbolSpec.point, {
      commissionPoints: this.cfg.commissionPoints || 0,
      fallbackSpreadPoints: this.cfg.fallbackSpreadPoints || 0,
    });
    if (!rng) {
      this.say(symbol, 'skipped',
        `no bars inside the ${clock.hhmm(new Date(open))}-${clock.hhmm(new Date(rangeEnd))} UTC `
        + `opening range`, true);
      return;
    }
    this.ranges.set(key, rng);
    this.status.set(symbol, 'watching');

    if (this.now() > strategy.entryDeadline(rng, cfg, spec)) {
      this.status.set(symbol, 'window closed');
      return;
    }

    const adr = strategy.averageDailyRangePoints(
      bars, spec, symbolSpec.point, cfg.adrDays, clock.dateKey(localDate));
    const verdict = strategy.checkRange(rng, cfg, adr);
    if (!verdict.ok) {
      this.status.set(symbol, 'not today');
      this.say(symbol, 'skipped', verdict.reason, true);
      // Done for the day: the range is fixed once formed, so re-checking it
      // every twenty seconds would only repeat the same refusal.
      this.entries.set(key, cfg.maxTradesPerSession);
      return;
    }

    // The last CLOSED bar. A bar in progress has no close yet, and treating its
    // running price as one trades signals that never existed in any backtest.
    const minute = Math.floor(this.now() / 60000) * 60000;
    const closed = bars.filter((b) => b.t >= rng.end && b.t < minute);
    if (!closed.length) return;
    const bar = closed[closed.length - 1];

    const plan = strategy.breakout(rng, bar, cfg, this.barred.get(key) || []);
    if (!plan) return;

    const planCheck = strategy.checkPlan(plan, cfg);
    if (!planCheck.ok) {
      this.say(symbol, 'skipped', planCheck.reason, true);
      return;
    }

    // Everything from here is priced off the FILL, not off the signal.
    //
    // The signal price is the breakout bar's close, which is a mid/bid figure.
    // A long actually fills at the ask, so its stop is a spread further away
    // than the plan assumed. Sizing from the signal therefore places a position
    // whose real risk exceeds the budget by the spread as a fraction of the
    // stop -- 2% over on a 250-point stop at a 10-point spread, and much worse
    // on a tight stop. This mirrors what Executor.open does on the Python side.
    const quote = await this.adapter.quote(symbol);
    const fill = plan.direction === BUY ? quote.ask : quote.bid;
    const priced = strategy.stopAndTarget(rng, cfg, plan.direction, fill);
    if ((plan.direction === BUY && priced.stop >= fill)
      || (plan.direction === SELL && priced.stop <= fill)) {
      this.say(symbol, 'skipped',
        'the market moved through the stop before the order could be priced', true);
      return;
    }

    const riskPoints = Math.abs(fill - priced.stop) / symbolSpec.point;
    const lots = strategy.sizeForRisk(equity, this.limits.riskFraction,
      fill, priced.stop, symbolSpec);
    if (!(lots > 0)) {
      this.say(symbol, 'skipped',
        `a ${(this.limits.riskFraction * 100).toFixed(2)}% risk on ${equity.toFixed(2)} is `
        + `smaller than the ${symbolSpec.minLots} lot minimum at a `
        + `${riskPoints.toFixed(0)}pt stop`, true);
      return;
    }

    const order = {
      symbol, direction: plan.direction, lots, sl: priced.stop, tp: priced.target,
      comment: `ORB ${plan.direction === BUY ? 'BUY' : 'SELL'} ${rng.widthPoints.toFixed(0)}pt`,
    };

    if (this.limits.dryRun) {
      this.recordEntry(key, cfg, plan);
      this.say(symbol, 'opened',
        `dry run: would ${order.comment} ${lots} lots @ ${fill} `
        + `stop ${order.sl} target ${order.tp} -- ${plan.reason}`);
      return;
    }

    if (this.limits.confirmEveryOrder) {
      const ok = await this.confirm(order, plan);
      if (!ok) {
        this.say(symbol, 'skipped', 'you declined this order', false);
        return;
      }
    }

    const result = await this.adapter.placeOrder(order);
    if (result.ok) {
      this.recordEntry(key, cfg, plan);
      this.day.trades += 1;
      if (result.id !== undefined) {
        const seen = { id: result.id, symbol, direction: plan.direction, lots,
          entry: fill, sl: order.sl, tp: order.tp, profit: 0,
          orbReason: plan.reason };
        this.watching.set(result.id, seen);
      }
      this.status.set(symbol, 'in a trade');
      this.say(symbol, 'opened',
        `${order.comment} ${lots} lots @ ${fill} stop ${order.sl} `
        + `target ${order.tp} -- ${plan.reason}`);
    } else {
      this.say(symbol, 'error', `not opened: ${result.reason}`, true);
    }
  }

  recordEntry(key, cfg, plan) {
    this.entries.set(key, (this.entries.get(key) || 0) + 1);
    const barred = (this.barred.get(key) || []).concat(
      strategy.barredAfter(cfg, plan.direction));
    this.barred.set(key, barred);
  }

  // ------------------------------------------------------------- reporting
  get stats() {
    const closed = this.trades;
    const wins = closed.filter((t) => t.profit > 0);
    const losses = closed.filter((t) => t.profit < 0);
    const net = closed.reduce((a, t) => a + t.profit, 0);
    const best = closed.reduce((a, t) => Math.max(a, t.profit), 0);
    const worst = closed.reduce((a, t) => Math.min(a, t.profit), 0);
    return {
      total: closed.length,
      wins: wins.length,
      losses: losses.length,
      winRate: closed.length ? wins.length / closed.length : 0,
      net,
      best,
      worst,
    };
  }

  // Trades this bot managed since the page was loaded -- not account history.
  // Said explicitly in the panel too, because a session-scoped counter read as
  // a track record is how five trades becomes a 100% win rate.
  toCsv() {
    const header = 'time_utc,symbol,side,lots,entry,profit,reason';
    const rows = this.trades.map((t) => [
      new Date(t.at).toISOString(), t.symbol,
      t.direction === BUY ? 'BUY' : 'SELL', t.lots, t.entry,
      t.profit.toFixed(2), JSON.stringify(t.reason || ''),
    ].join(','));
    return [header].concat(rows).join('\n');
  }
}

const api = { Bot, DEFAULT_LIMITS };

if (typeof module !== 'undefined' && module.exports) module.exports = api;
if (typeof globalThis !== 'undefined') globalThis.OrbBot = api;
