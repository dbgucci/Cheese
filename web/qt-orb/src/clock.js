// Session opens, in the exchange's time zone rather than in UTC.
//
// This is the JavaScript half of src/cheese_signals/markets/clock.py, and it
// exists for the same reason: the New York cash open is 09:30 America/New_York
// all year, which is 13:30 UTC in summer and 14:30 UTC in winter. A session
// hardcoded as a UTC hour is wrong for about five months of the year, and the
// failure is silent -- the bot builds its opening range over the wrong fifteen
// minutes and then trades breakouts of a level that means nothing.
//
// The browser has a full time zone database behind `Intl`, so none of this
// needs a library. What it does need is care: `Intl` converts an instant into a
// zone's wall clock, and what we want is the opposite, so the offset has to be
// resolved against the instant it applies to.

'use strict';

// Presets, matching SESSIONS in clock.py so the two cannot disagree about when
// a market opens.
const SESSIONS = {
  us_cash: { key: 'us_cash', tz: 'America/New_York', open: [9, 30], close: [16, 0], label: 'US cash equities' },
  london: { key: 'london', tz: 'Europe/London', open: [8, 0], close: [16, 30], label: 'London' },
  comex: { key: 'comex', tz: 'America/New_York', open: [8, 20], close: [13, 30], label: 'COMEX metals floor' },
  ny_fx: { key: 'ny_fx', tz: 'America/New_York', open: [8, 0], close: [17, 0], label: 'New York FX' },
  tokyo: { key: 'tokyo', tz: 'Asia/Tokyo', open: [9, 0], close: [15, 0], label: 'Tokyo' },
};

// Which session each instrument's opening range belongs to. Indices go to their
// own cash open; metals and FX default to London.
const INSTRUMENT_SESSIONS = {
  US30: 'us_cash', DJ30: 'us_cash', WS30: 'us_cash', DOW: 'us_cash',
  SPX500: 'us_cash', US500: 'us_cash', SP500: 'us_cash',
  NAS100: 'us_cash', USTEC: 'us_cash', NDX100: 'us_cash',
  US2000: 'us_cash', RUSSELL2000: 'us_cash',
  GER40: 'london', DE40: 'london', DAX40: 'london', UK100: 'london',
  FRA40: 'london', EU50: 'london', STOXX50: 'london',
  JP225: 'tokyo', JPN225: 'tokyo',
  XAUUSD: 'london', GOLD: 'london', XAGUSD: 'london', SILVER: 'london',
  XPTUSD: 'london', XPDUSD: 'london',
  EURUSD: 'london', GBPUSD: 'london', USDCHF: 'london', USDJPY: 'london',
  AUDUSD: 'london', NZDUSD: 'london', USDCAD: 'london', EURGBP: 'london',
  EURJPY: 'london', GBPJPY: 'london', AUDJPY: 'london', EURAUD: 'london',
  EURCHF: 'london', GBPCHF: 'london', CADJPY: 'london', CHFJPY: 'london',
  NZDJPY: 'london', AUDNZD: 'london', AUDCAD: 'london', EURCAD: 'london',
  GBPCAD: 'london', GBPAUD: 'london', USDSGD: 'london',
};

const INDEX_PREFIXES = [
  'US30', 'DJ30', 'WS30', 'DOW', 'SPX', 'US500', 'SP500', 'NAS', 'USTEC',
  'NDX', 'US2000', 'RUSSELL', 'GER', 'DE40', 'DAX', 'UK100', 'FRA', 'EU50',
  'STOXX', 'JP225', 'JPN225',
];

// A broker symbol reduced to the instrument it actually is.
//
// This platform's symbols are decorated in ways a table of exact names cannot
// see: the recording that prompted this file was trading `XAUUSD247`, and
// `XAUUSD.r`, `US30cash` and `NAS100_m` are all normal elsewhere. Longest match
// first, so SPX500 is not shadowed by a shorter key that prefixes it.
function baseName(symbol) {
  const cleaned = String(symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
  const keys = Object.keys(INSTRUMENT_SESSIONS).sort((a, b) => b.length - a.length);
  for (const key of keys) {
    if (cleaned.startsWith(key)) return key;
  }
  return cleaned;
}

function isIndex(symbol) {
  const name = baseName(symbol);
  return INDEX_PREFIXES.some((p) => name.startsWith(p));
}

// Indices open with a genuine auction, so fifteen minutes is the standard
// anchor. Spot FX and metals have no auction -- London "opens" as a gradual
// handover from Asia -- so a fifteen-minute window on a slow handover produces
// a range too narrow to mean anything. Thirty is the adjustment.
function suggestRangeMinutes(symbol) {
  return isIndex(symbol) ? 15 : 30;
}

function sessionFor(symbol, override) {
  if (override) {
    if (!SESSIONS[override]) {
      throw new Error(`unknown session '${override}'; choose from ${Object.keys(SESSIONS).join(', ')}`);
    }
    return SESSIONS[override];
  }
  const key = INSTRUMENT_SESSIONS[baseName(symbol)];
  if (!key) {
    // Guessing "London, probably" is how a bot trades the opening range of a
    // market that was closed at the time.
    throw new Error(`no session mapped for ${symbol} (read as '${baseName(symbol)}')`);
  }
  return SESSIONS[key];
}

// --------------------------------------------------------------------------
// time zone arithmetic
// --------------------------------------------------------------------------
const _FORMATTERS = new Map();

function _formatter(timeZone) {
  if (!_FORMATTERS.has(timeZone)) {
    _FORMATTERS.set(timeZone, new Intl.DateTimeFormat('en-US', {
      timeZone, hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }));
  }
  return _FORMATTERS.get(timeZone);
}

// The zone's wall clock at a given instant, as plain numbers.
function wallClock(instant, timeZone) {
  const parts = {};
  for (const p of _formatter(timeZone).formatToParts(instant)) {
    if (p.type !== 'literal') parts[p.type] = Number(p.value);
  }
  // `hour12: false` yields hour 24 for midnight in some engines.
  return {
    year: parts.year, month: parts.month, day: parts.day,
    hour: parts.hour % 24, minute: parts.minute, second: parts.second,
  };
}

// How far ahead of UTC the zone is, at that instant, in minutes.
function offsetMinutes(instant, timeZone) {
  const w = wallClock(instant, timeZone);
  const asUtc = Date.UTC(w.year, w.month - 1, w.day, w.hour, w.minute, w.second);
  return (asUtc - instant.getTime() + (instant.getMilliseconds())) / 60000;
}

// The UTC instant at which a zone's clock reads the given wall time.
//
// Resolved twice on purpose. The offset depends on the instant, and the instant
// is what we are solving for, so the first pass uses an approximate offset and
// the second corrects it. Without the second pass every date within a day of a
// daylight-saving transition comes out an hour wrong -- which is precisely the
// week this whole module exists to get right.
function zonedTimeToUtc(year, month, day, hour, minute, timeZone) {
  const naive = Date.UTC(year, month - 1, day, hour, minute, 0);
  let guess = new Date(naive - offsetMinutes(new Date(naive), timeZone) * 60000);
  guess = new Date(naive - offsetMinutes(guess, timeZone) * 60000);
  return guess;
}

// The session open on a given exchange-local date, as a UTC Date.
function sessionOpen(spec, localDate) {
  const [h, m] = spec.open;
  return zonedTimeToUtc(localDate.year, localDate.month, localDate.day, h, m, spec.tz);
}

function sessionClose(spec, localDate) {
  const [h, m] = spec.close;
  const [oh, om] = spec.open;
  // A session whose close is earlier than its open wraps past midnight.
  const wraps = h * 60 + m <= oh * 60 + om;
  let { year, month, day } = localDate;
  if (wraps) {
    const next = new Date(Date.UTC(year, month - 1, day + 1));
    year = next.getUTCFullYear();
    month = next.getUTCMonth() + 1;
    day = next.getUTCDate();
  }
  return zonedTimeToUtc(year, month, day, h, m, spec.tz);
}

// Which trading day a UTC instant belongs to, in the exchange's own calendar.
//
// Not UTC's date. An instant at 00:30 UTC on Wednesday is part of Tuesday's New
// York session, and grouping it under Wednesday splits one session's bars
// across two days.
function sessionDate(spec, instant) {
  const w = wallClock(instant, spec.tz);
  return { year: w.year, month: w.month, day: w.day };
}

function isWeekday(localDate) {
  const d = new Date(Date.UTC(localDate.year, localDate.month - 1, localDate.day));
  const dow = d.getUTCDay();
  return dow >= 1 && dow <= 5;
}

function dateKey(localDate) {
  const p = (n) => String(n).padStart(2, '0');
  return `${localDate.year}-${p(localDate.month)}-${p(localDate.day)}`;
}

function hhmm(instant) {
  const p = (n) => String(n).padStart(2, '0');
  return `${p(instant.getUTCHours())}:${p(instant.getUTCMinutes())}`;
}

const api = {
  SESSIONS, INSTRUMENT_SESSIONS, INDEX_PREFIXES,
  baseName, isIndex, suggestRangeMinutes, sessionFor,
  wallClock, offsetMinutes, zonedTimeToUtc,
  sessionOpen, sessionClose, sessionDate, isWeekday, dateKey, hhmm,
};

if (typeof module !== 'undefined' && module.exports) module.exports = api;
if (typeof globalThis !== 'undefined') globalThis.OrbClock = api;
