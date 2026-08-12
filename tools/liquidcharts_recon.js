/*
 * Liquid Charts reconnaissance -- READ ONLY.
 *
 * Paste into the browser console on trader.liquidcharts.com with a chart
 * open, let it watch for a minute, then run  kpsRecon.report()  and send the
 * output back.
 *
 * This exists because a bot that drives a web platform has to know three
 * things, none of which can be guessed from outside:
 *
 *   1. where live prices come from  (a WebSocket, a poll, or only the canvas)
 *   2. whether the app exposes a usable object, or only pixels
 *   3. what the order controls are, precisely enough to find them again
 *      after a page reload
 *
 * It places nothing, clicks nothing, and sends nothing anywhere. It only
 * watches traffic the page is already making and reads the DOM. Read the
 * source before running it -- you should never paste a script into a page
 * holding your account without doing that, least of all one handed to you by
 * a stranger promising a trading bot.
 */

(function () {
  "use strict";

  const state = {
    sockets: [],          // {url, sent, received, samples[]}
    fetches: new Map(),   // url pattern -> count
    startedAt: new Date(),
  };

  // ---------------------------------------------------------------- sockets
  // Most trading web apps stream quotes over a WebSocket. If one exists, it
  // is by far the best price source: it is the same data the chart draws, it
  // arrives as structured JSON, and reading it needs no scraping at all.
  const NativeWS = window.WebSocket;
  window.WebSocket = function (url, protocols) {
    const ws = protocols ? new NativeWS(url, protocols) : new NativeWS(url);
    const rec = { url: String(url), sent: 0, received: 0, samples: [] };
    state.sockets.push(rec);

    ws.addEventListener("message", (ev) => {
      rec.received += 1;
      if (rec.samples.length < 5 && typeof ev.data === "string") {
        rec.samples.push(ev.data.slice(0, 400));
      }
    });
    const send = ws.send.bind(ws);
    ws.send = function (data) {
      rec.sent += 1;
      if (rec.samples.length < 8 && typeof data === "string") {
        rec.samples.push("SENT: " + data.slice(0, 300));
      }
      return send(data);
    };
    return ws;
  };
  window.WebSocket.prototype = NativeWS.prototype;

  // ------------------------------------------------------------------ fetch
  const nativeFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : input.url;
      const key = String(url).split("?")[0];
      state.fetches.set(key, (state.fetches.get(key) || 0) + 1);
    } catch (e) { /* never let instrumentation break the page */ }
    return nativeFetch.apply(this, arguments);
  };

  // ------------------------------------------------------------- app object
  // A platform that exposes its own trading object is the difference between
  // a bot that calls a function and a bot that clicks a button and hopes.
  function appGlobals() {
    const interesting = [];
    const skip = new Set(["WebSocket", "fetch", "kpsRecon"]);
    for (const key of Object.getOwnPropertyNames(window)) {
      if (skip.has(key) || key.startsWith("webkit")) continue;
      let v;
      try { v = window[key]; } catch (e) { continue; }
      if (!v || typeof v !== "object") continue;
      const name = key.toLowerCase();
      const looksRelevant =
        /trade|order|quote|price|chart|broker|account|position|symbol|market|socket|api|store|app/.test(name);
      if (!looksRelevant) continue;
      let keys = [];
      try { keys = Object.keys(v).slice(0, 25); } catch (e) { /* proxied */ }
      if (keys.length) interesting.push({ global: key, keys });
    }
    return interesting.slice(0, 40);
  }

  // ------------------------------------------------------------- DOM probes
  function describe(el) {
    if (!el) return null;
    const attrs = {};
    for (const a of el.attributes || []) {
      if (/^(id|class|name|type|placeholder|aria-label|title|data-[\w-]+)$/.test(a.name)) {
        attrs[a.name] = a.value.slice(0, 120);
      }
    }
    return {
      tag: el.tagName.toLowerCase(),
      text: (el.textContent || "").trim().slice(0, 60),
      attrs,
      selector: cssPath(el),
      visible: !!(el.offsetWidth || el.offsetHeight),
    };
  }

  // A selector the bot can use to find this control again tomorrow. Prefers
  // stable hooks (id, data-testid) over generated class names, which change
  // on every deploy of most front-end builds.
  function cssPath(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    for (const attr of ["data-testid", "data-test", "data-id", "name", "aria-label"]) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) return `${el.tagName.toLowerCase()}[${attr}="${CSS.escape(v)}"]`;
    }
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 4; depth += 1) {
      let part = node.tagName.toLowerCase();
      const cls = (node.className || "").toString().trim().split(/\s+/)
        .filter((c) => c && !/^[a-z]+-?\d/.test(c)).slice(0, 2);
      if (cls.length) part += "." + cls.join(".");
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(" > ");
  }

  function orderControls() {
    const wanted = /^(buy|sell|long|short|bid|ask|place order|market|confirm)$/i;
    const hits = [];
    document.querySelectorAll("button, [role=button], a, div[class*=btn], div[class*=button]")
      .forEach((el) => {
        const t = (el.textContent || "").trim();
        if (t.length > 24) return;
        if (wanted.test(t) || /buy|sell/i.test(el.className || "")) {
          hits.push(describe(el));
        }
      });
    return hits.slice(0, 30);
  }

  function inputs() {
    return Array.from(document.querySelectorAll("input, select"))
      .filter((el) => el.offsetWidth || el.offsetHeight)
      .slice(0, 30)
      .map(describe);
  }

  // Elements whose text looks like a live price and changes over time. The
  // ones that change are the quote; the ones that do not are labels.
  function priceCandidates() {
    const out = [];
    const re = /^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+\.\d{2,5}$/;
    document.querySelectorAll("span, div, td").forEach((el) => {
      if (el.children.length) return;
      const t = (el.textContent || "").trim();
      if (t.length > 14 || !re.test(t)) return;
      if (!(el.offsetWidth || el.offsetHeight)) return;
      out.push({ el, first: t });
    });
    return out.slice(0, 120);
  }

  const watched = priceCandidates();
  setTimeout(() => {
    watched.forEach((w) => { w.second = (w.el.textContent || "").trim(); });
  }, 8000);

  // ------------------------------------------------------------------ report
  window.kpsRecon = {
    report() {
      const changing = watched
        .filter((w) => w.second !== undefined && w.second !== w.first)
        .slice(0, 15)
        .map((w) => ({ ...describe(w.el), from: w.first, to: w.second }));

      const out = {
        page: location.href,
        watchedFor: Math.round((Date.now() - state.startedAt) / 1000) + "s",
        websockets: state.sockets.map((s) => ({
          url: s.url, received: s.received, sent: s.sent, samples: s.samples,
        })),
        httpEndpoints: Array.from(state.fetches.entries())
          .sort((a, b) => b[1] - a[1]).slice(0, 25),
        appGlobals: appGlobals(),
        orderControls: orderControls(),
        inputs: inputs(),
        livePriceElements: changing,
        canvasCount: document.querySelectorAll("canvas").length,
      };
      const text = JSON.stringify(out, null, 2);
      console.log(text);
      try {
        copy(out);
        console.log("%cCopied to clipboard. Paste it back to Claude.",
                    "color:#0a0;font-weight:bold");
      } catch (e) {
        console.log("Select the JSON above and copy it manually.");
      }
      return out;
    },
  };

  console.log("%cKPS recon armed. Read-only.", "color:#D4AF37;font-weight:bold");
  console.log("Leave this tab open and active for ~60s, switch instruments or "
            + "open the order ticket once, then run:  kpsRecon.report()");
})();
