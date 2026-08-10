// The draggable overlay panel.
//
// Same design language as the desktop apps in this repository -- true-black
// canvas, hairline dividers, gold as the only accent, green and red reserved
// strictly for direction and outcome -- so a green control is always a buy or a
// win and never "confirm". All styles are injected into a single scoped <style>
// tag under one root id, so nothing here can leak into the host page's CSS and
// the whole panel can be removed by deleting one node.
//
// The panel answers three questions continuously, because they are the ones a
// person supervising a live bot actually has:
//
//   Am I live?            Said in the header badge, and the start button turns
//                         red. A dry run mistaken for live wastes an afternoon;
//                         live mistaken for a dry run costs an account.
//   Why is it not
//   trading right now?    Every instrument carries a status and the last reason
//                         it was given. This is the question trading software
//                         almost never answers.
//   What has it actually
//   done?                 Session stats, labelled as session stats. A counter
//                         that resets on reload, read as a track record, is how
//                         five trades becomes "100% win rate".

'use strict';

const ROOT_ID = 'orb-autobot-panel';

const C = {
  bg: '#000000', surface: '#0F0F0F', surfaceAlt: '#161616',
  border: '#262626', borderSoft: '#1A1A1A',
  text: '#FAFAFA', muted: '#A8A8A8', faint: '#6B6B6B',
  gold: '#D4AF37', goldBright: '#EFC94C', goldDim: '#7A6420',
  buy: '#26A65B', sell: '#E0483A', warn: '#E0A030',
  font: '"Segoe UI Variable Display","Segoe UI",Inter,-apple-system,system-ui,sans-serif',
  mono: '"JetBrains Mono","Cascadia Mono","SF Mono",Consolas,monospace',
};

function css() {
  return `
#${ROOT_ID}{position:fixed;top:80px;left:80px;width:520px;max-height:86vh;z-index:2147483000;
  background:${C.bg};color:${C.text};font-family:${C.font};font-size:13px;
  border:1px solid ${C.border};border-radius:14px;display:flex;flex-direction:column;
  box-shadow:0 24px 60px rgba(0,0,0,.6);overflow:hidden}
#${ROOT_ID} *{box-sizing:border-box}
#${ROOT_ID} .orb-head{display:flex;align-items:center;gap:10px;padding:14px 16px;
  cursor:grab;border-bottom:1px solid ${C.borderSoft};background:${C.surface};user-select:none}
#${ROOT_ID} .orb-head.drag{cursor:grabbing}
#${ROOT_ID} .orb-title{font-weight:650;letter-spacing:.2px}
#${ROOT_ID} .orb-badge{font-size:10px;letter-spacing:1px;text-transform:uppercase;
  padding:3px 8px;border-radius:999px;border:1px solid ${C.goldDim};color:${C.gold}}
#${ROOT_ID} .orb-badge.live{border-color:${C.sell};color:${C.sell}}
#${ROOT_ID} .orb-spacer{flex:1}
#${ROOT_ID} .orb-icon{background:none;border:none;color:${C.faint};cursor:pointer;
  font-size:15px;line-height:1;padding:4px 6px;border-radius:6px}
#${ROOT_ID} .orb-icon:hover{color:${C.text};background:${C.surfaceAlt}}
#${ROOT_ID} .orb-body{overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:14px}
#${ROOT_ID} .orb-note{border:1px solid ${C.goldDim};background:rgba(212,175,55,.08);
  border-radius:10px;padding:10px 12px;color:${C.muted};line-height:1.5;font-size:12px}
#${ROOT_ID} .orb-note.danger{border-color:${C.sell};background:rgba(224,72,58,.10);color:#FFC9C3}
#${ROOT_ID} .orb-banner{border-radius:10px;padding:10px 12px;text-align:center;
  font-weight:600;letter-spacing:.3px;border:1px solid ${C.border};color:${C.faint}}
#${ROOT_ID} .orb-banner.on{border-color:${C.buy};color:${C.buy};background:rgba(38,166,91,.12)}
#${ROOT_ID} .orb-section{font-size:10px;letter-spacing:1.4px;text-transform:uppercase;
  color:${C.faint};margin-bottom:2px}
#${ROOT_ID} .orb-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
#${ROOT_ID} .orb-tile{background:${C.surface};border:1px solid ${C.borderSoft};
  border-radius:10px;padding:10px 12px}
#${ROOT_ID} .orb-tile .k{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:${C.faint}}
#${ROOT_ID} .orb-tile .v{font-size:20px;font-weight:650;margin-top:3px;font-family:${C.mono}}
#${ROOT_ID} .orb-row{display:flex;align-items:center;gap:8px}
#${ROOT_ID} label.orb-lab{flex:1;color:${C.muted};font-size:12px}
#${ROOT_ID} input[type=text],#${ROOT_ID} input[type=number],#${ROOT_ID} select{
  background:${C.surfaceAlt};color:${C.text};border:1px solid ${C.border};border-radius:8px;
  padding:7px 9px;font-family:${C.mono};font-size:12px;width:150px}
#${ROOT_ID} input:focus,#${ROOT_ID} select:focus{outline:none;border-color:${C.goldDim}}
#${ROOT_ID} button.orb-btn{background:${C.surfaceAlt};color:${C.text};border:1px solid ${C.border};
  border-radius:9px;padding:9px 14px;font-size:12px;font-weight:600;cursor:pointer}
#${ROOT_ID} button.orb-btn:hover{border-color:${C.faint}}
#${ROOT_ID} button.orb-btn.primary{background:${C.gold};color:#150F00;border-color:${C.gold}}
#${ROOT_ID} button.orb-btn.primary:hover{background:${C.goldBright}}
#${ROOT_ID} button.orb-btn.danger{background:${C.sell};color:#fff;border-color:${C.sell}}
#${ROOT_ID} button.orb-btn:disabled{opacity:.45;cursor:not-allowed}
#${ROOT_ID} table.orb-tbl{width:100%;border-collapse:collapse;font-size:11.5px}
#${ROOT_ID} table.orb-tbl th{text-align:left;color:${C.faint};font-weight:500;
  text-transform:uppercase;letter-spacing:.8px;font-size:9.5px;padding:5px 6px;
  border-bottom:1px solid ${C.borderSoft}}
#${ROOT_ID} table.orb-tbl td{padding:6px;border-bottom:1px solid ${C.borderSoft};
  color:${C.muted};vertical-align:top}
#${ROOT_ID} table.orb-tbl td.sym{color:${C.text};font-family:${C.mono}}
#${ROOT_ID} .orb-log{max-height:170px;overflow-y:auto;font-family:${C.mono};font-size:11px;
  background:${C.surface};border:1px solid ${C.borderSoft};border-radius:10px;padding:8px}
#${ROOT_ID} .orb-log div{padding:2px 0;white-space:pre-wrap;word-break:break-word}
#${ROOT_ID} .orb-foot{border-top:1px solid ${C.borderSoft};padding:9px 16px;
  display:flex;gap:8px;align-items:center;background:${C.surface};font-size:11px;color:${C.faint}}
#${ROOT_ID}.min .orb-body,#${ROOT_ID}.min .orb-foot{display:none}
#${ROOT_ID} details summary{cursor:pointer;color:${C.muted};font-size:12px;
  padding:4px 0;list-style:none}
#${ROOT_ID} details summary::-webkit-details-marker{display:none}
#${ROOT_ID} details summary:before{content:'▸ ';color:${C.faint}}
#${ROOT_ID} details[open] summary:before{content:'▾ '}
`;
}

const KIND_COLOUR = {
  opened: C.gold, closed: C.text, settled: C.text, trailed: C.goldDim,
  blocked: C.warn, halted: C.warn, warning: C.warn, error: C.sell,
  skipped: C.faint,
};

// The document the panel is mounted into. Set on mount, because nodes are also
// created long afterwards -- every log line and session row -- and a bare
// global `document` is the wrong one inside a sandboxed widget, and gone
// entirely once the host page has navigated away.
let DOC = null;

function el(tag, attrs, children) {
  const doc = DOC || (typeof document !== 'undefined' ? document : null);
  if (!doc) throw new Error('no document to build the panel in');
  const node = doc.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children || []) if (child) node.appendChild(child);
  return node;
}

function tile(key) {
  const value = el('div', { class: 'v', text: '--' });
  const node = el('div', { class: 'orb-tile' }, [el('div', { class: 'k', text: key }), value]);
  return { node, value };
}

function labelled(text, control) {
  return el('div', { class: 'orb-row' },
    [el('label', { class: 'orb-lab', text }), control]);
}

// --------------------------------------------------------------------------
class Panel {
  constructor(options) {
    const o = options || {};
    this.onStart = o.onStart || (() => {});
    this.onStop = o.onStop || (() => {});
    this.onExport = o.onExport || (() => {});
    this.onSettingsChanged = o.onSettingsChanged || (() => {});
    this.document = o.document || document;
    this.root = null;
    this.live = false;
    this.running = false;
  }

  mount(parent) {
    const doc = this.document;
    DOC = doc;
    const existing = doc.getElementById(ROOT_ID);
    if (existing) existing.remove();          // remounting must not stack panels

    const style = el('style', { text: css() });
    this.badge = el('span', { class: 'orb-badge', text: 'dry run' });
    const head = el('div', { class: 'orb-head' }, [
      el('span', { class: 'orb-title', text: 'ORB Autobot' }),
      this.badge,
      el('span', { class: 'orb-spacer' }),
      el('button', { class: 'orb-icon', text: '–', title: 'Minimise',
        onclick: () => this.root.classList.toggle('min') }),
      el('button', { class: 'orb-icon', text: '×', title: 'Remove',
        onclick: () => this.destroy() }),
    ]);

    this.warning = el('div', { class: 'orb-note danger', text:
      'Dry run: the bot decides and reports, and sends nothing. Untick it below '
      + 'only after you have watched a full session and read the log.' });
    this.banner = el('div', { class: 'orb-banner', text: 'STOPPED' });

    this.tiles = {
      total: tile('Trades this session'),
      winRate: tile('Win rate'),
      net: tile('Net P/L'),
      best: tile('Best / worst'),
    };
    const stats = el('div', { class: 'orb-grid' },
      Object.values(this.tiles).map((t) => t.node));

    this.symbolsInput = el('input', { type: 'text', value: 'XAUUSD247' });
    this.symbolsInput.style.width = '210px';
    this.riskInput = el('input', { type: 'number', value: '0.5', step: '0.05', min: '0.01' });
    this.targetInput = el('input', { type: 'number', value: '2', step: '0.25', min: '0.25' });
    this.dryRun = el('input', { type: 'checkbox' });
    this.dryRun.checked = true;
    this.confirmEach = el('input', { type: 'checkbox' });
    this.confirmEach.checked = true;

    this.startBtn = el('button', { class: 'orb-btn primary', text: 'Start bot',
      onclick: () => (this.running ? this.onStop() : this.onStart(this.settings())) });
    const exportBtn = el('button', { class: 'orb-btn', text: 'Export CSV',
      onclick: () => this.onExport() });

    this.sessionsBody = el('tbody', {});
    const sessions = el('table', { class: 'orb-tbl' }, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: 'Instrument' }), el('th', { text: 'Range (UTC)' }),
        el('th', { text: 'Status' }), el('th', { text: 'Why' })])]),
      this.sessionsBody,
    ]);

    this.logBox = el('div', { class: 'orb-log' });

    const advanced = el('details', {}, [
      el('summary', { text: 'Advanced' }),
      labelled('Confirm every order before it is sent', this.confirmEach),
      labelled('Take profit, in multiples of risk', this.targetInput),
      el('div', { class: 'orb-note', text:
        'Instruments: type them exactly as your own chart shows them (XAUUSD247, '
        + 'NAS100), not a nickname like "Gold". Suffixes are matched automatically.' }),
    ]);

    const body = el('div', { class: 'orb-body' }, [
      this.warning,
      this.banner,
      el('div', {}, [el('div', { class: 'orb-section', text: 'This session only' }), stats]),
      el('div', { class: 'orb-note', text:
        'Counts trades this bot has managed since the page was loaded — not your '
        + 'account history. Export before closing the tab.' }),
      el('div', {}, [
        el('div', { class: 'orb-section', text: 'Setup' }),
        labelled('Instruments, comma separated', this.symbolsInput),
        labelled('Risk per trade, % of equity', this.riskInput),
        labelled('Dry run (send nothing)', this.dryRun),
        advanced,
      ]),
      el('div', { class: 'orb-row' }, [this.startBtn, exportBtn]),
      el('div', {}, [el('div', { class: 'orb-section', text: "Today's sessions" }), sessions]),
      el('div', {}, [el('div', { class: 'orb-section', text: 'Activity' }), this.logBox]),
    ]);

    this.statusText = el('span', { text: 'Not started.' });
    const foot = el('div', { class: 'orb-foot' }, [this.statusText]);

    this.root = el('div', { id: ROOT_ID }, [style, head, body, foot]);
    (parent || doc.body).appendChild(this.root);
    this._makeDraggable(head);
    this.dryRun.addEventListener('change', () => {
      this.setLive(!this.dryRun.checked);
      this.onSettingsChanged(this.settings());
    });
    return this;
  }

  _makeDraggable(handle) {
    let startX = 0;
    let startY = 0;
    let originLeft = 0;
    let originTop = 0;
    const move = (e) => {
      const left = originLeft + (e.clientX - startX);
      const top = originTop + (e.clientY - startY);
      // Kept on screen: a panel dragged off the edge of a chart cannot be
      // dragged back, and the host page owns the scrollbars.
      const maxLeft = Math.max(0, window.innerWidth - this.root.offsetWidth);
      const maxTop = Math.max(0, window.innerHeight - 40);
      this.root.style.left = `${Math.min(Math.max(0, left), maxLeft)}px`;
      this.root.style.top = `${Math.min(Math.max(0, top), maxTop)}px`;
    };
    const up = () => {
      handle.classList.remove('drag');
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    handle.addEventListener('mousedown', (e) => {
      if (e.target.classList.contains('orb-icon')) return;
      startX = e.clientX;
      startY = e.clientY;
      const box = this.root.getBoundingClientRect();
      originLeft = box.left;
      originTop = box.top;
      handle.classList.add('drag');
      window.addEventListener('mousemove', move);
      window.addEventListener('mouseup', up);
      e.preventDefault();
    });
  }

  settings() {
    return {
      symbols: this.symbolsInput.value.split(',').map((s) => s.trim().toUpperCase())
        .filter(Boolean),
      riskFraction: Math.max(0.0001, Number(this.riskInput.value) / 100),
      targetR: Math.max(0.25, Number(this.targetInput.value)),
      dryRun: this.dryRun.checked,
      confirmEveryOrder: this.confirmEach.checked,
    };
  }

  setLive(live) {
    this.live = live;
    this.badge.textContent = live ? 'LIVE' : 'dry run';
    this.badge.classList.toggle('live', live);
    this.warning.textContent = live
      ? 'LIVE: real orders will be sent to your account the moment conditions are met.'
      : 'Dry run: the bot decides and reports, and sends nothing. Untick it below '
        + 'only after you have watched a full session and read the log.';
  }

  setRunning(running, symbolLabel) {
    this.running = running;
    this.banner.textContent = running
      ? `● BOT RUNNING — ${symbolLabel || ''}`.trim() : 'STOPPED';
    this.banner.classList.toggle('on', running);
    this.startBtn.textContent = running ? 'Stop bot' : 'Start bot';
    this.startBtn.classList.toggle('danger', running);
    this.startBtn.classList.toggle('primary', !running);
    for (const input of [this.symbolsInput, this.riskInput, this.dryRun, this.confirmEach]) {
      input.disabled = running;
    }
  }

  setStatus(text) {
    this.statusText.textContent = text;
  }

  setStats(stats, currency) {
    const money = (n) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}`;
    this.tiles.total.value.textContent = String(stats.total);
    this.tiles.winRate.value.textContent = stats.total
      ? `${(stats.winRate * 100).toFixed(1)}%` : '--';
    this.tiles.net.value.textContent = stats.total ? money(stats.net) : '--';
    this.tiles.net.value.style.color = stats.net > 0 ? C.buy : stats.net < 0 ? C.sell : C.text;
    this.tiles.best.value.textContent = stats.total
      ? `${money(stats.best)} / ${stats.worst ? money(stats.worst) : '—'}` : '--';
    if (currency) this.tiles.net.node.querySelector('.k').textContent = `Net P/L (${currency})`;
  }

  setSessions(rows) {
    this.sessionsBody.innerHTML = '';
    for (const row of rows) {
      this.sessionsBody.appendChild(el('tr', {}, [
        el('td', { class: 'sym', text: row.symbol }),
        el('td', { text: row.window || '--' }),
        el('td', { text: row.status || '' }),
        el('td', { text: row.detail || '' }),
      ]));
    }
  }

  log(event) {
    const time = new Date(event.at).toISOString().slice(11, 19);
    const line = el('div', {
      text: `${time}  ${event.symbol}  ${event.kind}  ${event.detail}`,
    });
    line.style.color = KIND_COLOUR[event.kind] || C.muted;
    this.logBox.insertBefore(line, this.logBox.firstChild);
    while (this.logBox.childNodes.length > 400) {
      this.logBox.removeChild(this.logBox.lastChild);
    }
  }

  destroy() {
    if (this.root) this.root.remove();
    this.root = null;
  }
}

const api = { Panel, ROOT_ID, COLOURS: C };

if (typeof module !== 'undefined' && module.exports) module.exports = api;
if (typeof globalThis !== 'undefined') globalThis.OrbPanel = api;
