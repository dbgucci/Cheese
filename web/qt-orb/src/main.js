// Entry point: wires the panel to the bot, and the bot to whatever platform
// this page turns out to expose.
//
// Kept last in the bundle and deliberately thin. Everything it touches is
// already tested on its own; what happens here is the part that can only be
// verified in the page itself.

'use strict';

(function () {
  const clock = require('./clock.js');
  const strategy = require('./strategy.js');
  const adapters = require('./adapter.js');
  const { Bot } = require('./bot.js');
  const { Panel } = require('./panel.js');

  const POLL_MS = 15000;

  function pickAdapter(log) {
    // Explicitly installed adapter wins: this is the hook for wiring Figaro up
    // without rebuilding, and for anyone testing against their own shim.
    if (globalThis.ORB_ADAPTER) {
      log(`using the adapter installed as window.ORB_ADAPTER `
        + `(${globalThis.ORB_ADAPTER.name || 'unnamed'})`);
      return globalThis.ORB_ADAPTER;
    }
    // Otherwise: report what the page has, and refuse to trade rather than
    // guessing at an API. A wrong guess here does not fail safely -- it can
    // fail *after* sending a malformed order.
    const found = adapters.discoverPlatform(globalThis);
    log('No platform adapter is installed, so the bot cannot reach your account.');
    log('This build ships the strategy, the risk limits and the panel; the eight '
      + 'calls that touch the platform are left unwired rather than guessed.');
    if (found.length) {
      log(`Page globals that look relevant: ${found.map((f) => f.key).join(', ')}`);
      log('Run  copy(JSON.stringify(OrbAdapter.discoverPlatform(window), null, 2))  '
        + 'in the console and send the result to have the adapter written.');
    }
    return null;
  }

  function start() {
    const panel = new Panel({});
    let bot = null;
    let timer = null;

    const log = (text, kind) => panel.log({
      at: Date.now(), symbol: '-', kind: kind || 'info', detail: text });

    panel.mount();
    panel.setStatus('Ready. Nothing is sent while Dry run is ticked.');

    const adapter = pickAdapter(log);
    if (!adapter) {
      panel.setStatus('No platform adapter — dry run only.');
      panel.startBtn.disabled = true;
    }

    async function refresh() {
      // A removed panel must not keep polling: the interval outlives the DOM
      // node, and every tick after that throws inside the host page's console.
      if (!bot || !panel.root) {
        if (timer) clearInterval(timer);
        timer = null;
        return;
      }
      try {
        await bot.cycle();
      } catch (err) {
        panel.log({ at: Date.now(), symbol: '-', kind: 'error', detail: err.message });
      }
      let currency = '';
      try {
        currency = (await bot.adapter.account()).currency;
      } catch (e) { /* the cycle already reported it */ }
      panel.setStats(bot.stats, currency);
      panel.setSessions(sessionRows(bot));
    }

    function sessionRows(instance) {
      const now = Date.now();
      return instance.symbols.map((symbol) => {
        let window = '--';
        try {
          const spec = clock.sessionFor(symbol);
          const day = clock.sessionDate(spec, new Date(now));
          const open = clock.sessionOpen(spec, day);
          const minutes = clock.suggestRangeMinutes(symbol);
          window = `${clock.hhmm(open)}-${clock.hhmm(new Date(open.getTime() + minutes * 60000))}`;
        } catch (e) { window = 'unmapped'; }
        return {
          symbol,
          window,
          status: instance.status.get(symbol) || 'waiting',
          detail: instance.lastSaid.get(symbol) || '',
        };
      });
    }

    panel.onStart = (settings) => {
      if (!adapter) return;
      try {
        bot = new Bot({
          adapter,
          symbols: settings.symbols,
          strategy: { targetR: settings.targetR },
          limits: {
            riskFraction: settings.riskFraction,
            dryRun: settings.dryRun,
            confirmEveryOrder: settings.confirmEveryOrder,
          },
          onEvent: (e) => panel.log(e),
          confirm: async (order) => globalThis.confirm(
            `Send this order?\n\n${order.comment}\n${order.symbol} `
            + `${order.direction > 0 ? 'BUY' : 'SELL'} ${order.lots} lots\n`
            + `stop ${order.sl}\ntarget ${order.tp}`),
        });
      } catch (err) {
        panel.log({ at: Date.now(), symbol: '-', kind: 'error', detail: err.message });
        return;
      }
      panel.setRunning(true, settings.symbols.join(', '));
      panel.setStatus(settings.dryRun
        ? 'Running as a dry run — nothing is being sent.'
        : 'Running LIVE — real orders will be sent.');
      refresh();
      timer = setInterval(refresh, POLL_MS);
    };

    panel.onStop = () => {
      if (timer) clearInterval(timer);
      timer = null;
      panel.setRunning(false);
      panel.setStatus('Stopped. Open positions keep their stop and target, but '
        + 'nothing will flatten them at the session close now.');
    };

    panel.onExport = () => {
      if (!bot || !bot.trades.length) {
        panel.setStatus('Nothing to export yet.');
        return;
      }
      const blob = new Blob([bot.toCsv()], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `orb-session-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    };

    log(`ORB Autobot ready. Strategy defaults: ${strategy.DEFAULTS.rangeMinutes}min range `
      + `for indices, 30min for FX and metals, ${strategy.DEFAULTS.targetR}R target, `
      + `one trade and one direction per session.`);
    globalThis.ORB = { panel, get bot() { return bot; }, clock, strategy, adapters };
    log('Handles for the console are on window.ORB');
  }

  if (typeof document === 'undefined') return;

  // Gated on `document.body`, not on `document.readyState`.
  //
  // readyState is the obvious check and it is wrong here. A page can report
  // 'loading' with a body already in place, and this script is pasted by hand --
  // so it can easily arrive *after* DOMContentLoaded has fired, leaving a
  // listener that will never be called and a bot that silently never starts
  // with no error to explain it. What actually matters is whether there is a
  // body to mount into.
  if (document.body) {
    start();
  } else {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  }
})();
