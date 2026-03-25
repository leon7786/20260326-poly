#!/usr/bin/env node

const fs = require('fs');
const path = require('path');

const USER_AGENT = 'openclaw-crypto-paperbot-multi/0.3';
const GAMMA_BASE = 'https://gamma-api.polymarket.com';
const POLY_WS = 'wss://ws-subscriptions-clob.polymarket.com/ws/market';
const ROUND_MS = 15 * 60 * 1000;
const PRIMARY_SOURCES = ['binance', 'okx', 'coinbase'];
const SOURCE_WEIGHTS = { binance: 0.45, okx: 0.35, coinbase: 0.20 };

const SYMBOLS = {
  BTC: {
    label: 'Bitcoin',
    match: /(bitcoin|\bbtc\b)/i,
    sources: {
      binance: { url: 'wss://stream.binance.com:9443/ws/btcusdt@depth@100ms' },
      coinbase: { url: 'wss://advanced-trade-ws.coinbase.com', product: 'BTC-USD' },
      okx: { url: 'wss://ws.okx.com:8443/ws/v5/public', instId: 'BTC-USDT', channel: 'books' },
    },
  },
  ETH: {
    label: 'Ethereum',
    match: /(ethereum|\beth\b)/i,
    sources: {
      binance: { url: 'wss://stream.binance.com:9443/ws/ethusdt@depth@100ms' },
      coinbase: { url: 'wss://advanced-trade-ws.coinbase.com', product: 'ETH-USD' },
      okx: { url: 'wss://ws.okx.com:8443/ws/v5/public', instId: 'ETH-USDT', channel: 'books' },
    },
  },
  SOL: {
    label: 'Solana',
    match: /(solana|\bsol\b)/i,
    sources: {
      binance: { url: 'wss://stream.binance.com:9443/ws/solusdt@depth@100ms' },
      coinbase: { url: 'wss://advanced-trade-ws.coinbase.com', product: 'SOL-USD' },
      okx: { url: 'wss://ws.okx.com:8443/ws/v5/public', instId: 'SOL-USDT', channel: 'books' },
    },
  },
  XRP: {
    label: 'XRP',
    match: /(\bxrp\b|ripple)/i,
    sources: {
      binance: { url: 'wss://stream.binance.com:9443/ws/xrpusdt@depth@100ms' },
      coinbase: { url: 'wss://advanced-trade-ws.coinbase.com', product: 'XRP-USD' },
      okx: { url: 'wss://ws.okx.com:8443/ws/v5/public', instId: 'XRP-USDT', channel: 'books' },
    },
  },
  DOGE: {
    label: 'Dogecoin',
    match: /(dogecoin|\bdoge\b)/i,
    sources: {
      binance: { url: 'wss://stream.binance.com:9443/ws/dogeusdt@depth@100ms' },
      coinbase: { url: 'wss://advanced-trade-ws.coinbase.com', product: 'DOGE-USD' },
      okx: { url: 'wss://ws.okx.com:8443/ws/v5/public', instId: 'DOGE-USDT', channel: 'books' },
    },
  },
  HYPE: {
    label: 'Hyperliquid',
    match: /(hyperliquid|\bhype\b)/i,
    sources: {
      okx: { url: 'wss://ws.okx.com:8443/ws/v5/public', instId: 'HYPE-USDT', channel: 'books' },
    },
  },
  BNB: {
    label: 'BNB',
    match: /(binance coin|\bbnb\b)/i,
    sources: {
      binance: { url: 'wss://stream.binance.com:9443/ws/bnbusdt@depth20@100ms' },
      okx: { url: 'wss://ws.okx.com:8443/ws/v5/public', instId: 'BNB-USDT', channel: 'books' },
    },
  },
};

const STRATEGY_PROFILES = {
  BTC: { minSourceCount: 2, minEdge: 0.03, minQuality: 6.4, maxFlips: 2, maxSpreadBps: 4.2, moveScaleBps: 7.0, momScaleBps: 12.0, minRoundMoveBps: 2.2, minSecondsSinceLastFlip: 90, maxStakePct: 0.18, confidenceCap: 0.78, flipPenalty: 0.025, minEstimatedWinProb: 0.60 },
  ETH: { minSourceCount: 2, minEdge: 0.035, minQuality: 5.9, maxFlips: 2, maxSpreadBps: 4.5, moveScaleBps: 8.5, momScaleBps: 14.0, minRoundMoveBps: 2.5, minSecondsSinceLastFlip: 85, maxStakePct: 0.16, confidenceCap: 0.76, flipPenalty: 0.028, minEstimatedWinProb: 0.61 },
  SOL: { minSourceCount: 2, minEdge: 0.038, minQuality: 5.8, maxFlips: 2, maxSpreadBps: 5.0, moveScaleBps: 9.5, momScaleBps: 17.0, minRoundMoveBps: 2.8, minSecondsSinceLastFlip: 80, maxStakePct: 0.15, confidenceCap: 0.75, flipPenalty: 0.03, minEstimatedWinProb: 0.61 },
  XRP: { minSourceCount: 2, minEdge: 0.045, minQuality: 6.1, maxFlips: 2, maxSpreadBps: 4.8, moveScaleBps: 8.5, momScaleBps: 16.0, minRoundMoveBps: 2.7, minSecondsSinceLastFlip: 90, maxStakePct: 0.12, confidenceCap: 0.72, flipPenalty: 0.032, minEstimatedWinProb: 0.63 },
  DOGE: { minSourceCount: 2, minEdge: 0.05, minQuality: 6.8, maxFlips: 1, maxSpreadBps: 4.6, moveScaleBps: 10.5, momScaleBps: 18.0, minRoundMoveBps: 3.3, minSecondsSinceLastFlip: 110, maxStakePct: 0.10, confidenceCap: 0.70, flipPenalty: 0.04, minEstimatedWinProb: 0.64 },
  HYPE: { minSourceCount: 1, minEdge: 0.06, minQuality: 7.5, maxFlips: 1, maxSpreadBps: 3.5, moveScaleBps: 12.0, momScaleBps: 20.0, minRoundMoveBps: 3.8, minSecondsSinceLastFlip: 120, maxStakePct: 0.08, confidenceCap: 0.66, flipPenalty: 0.045, minEstimatedWinProb: 0.64 },
  BNB: { minSourceCount: 2, minEdge: 0.045, minQuality: 6.2, maxFlips: 2, maxSpreadBps: 4.5, moveScaleBps: 8.0, momScaleBps: 14.0, minRoundMoveBps: 2.6, minSecondsSinceLastFlip: 90, maxStakePct: 0.12, confidenceCap: 0.72, flipPenalty: 0.03, minEstimatedWinProb: 0.62 },
};

function strategyProfile(symbol) {
  return STRATEGY_PROFILES[symbol] || { minSourceCount: 2, minEdge: 0.04, minQuality: 6.0, maxFlips: 2, maxSpreadBps: 4.8, moveScaleBps: 9.0, momScaleBps: 15.0, minRoundMoveBps: 2.5, minSecondsSinceLastFlip: 90, maxStakePct: 0.12, confidenceCap: 0.72, flipPenalty: 0.03, minEstimatedWinProb: 0.62 };
}
function availableSourceCount(symbol) {
  return Object.keys(SYMBOLS[symbol]?.sources || {}).length || PRIMARY_SOURCES.length;
}
function isDirectionalShortHorizonMarket(text) {
  return /up\s*or\s*down|up\s*\/\s*down|higher\s*or\s*lower|higher\s*\/\s*lower|above\s*or\s*below|above\s*\/\s*below/i.test(text || '');
}
function isQuarterHourAligned(ts) {
  const d = new Date(ts);
  return Number.isFinite(ts) && d.getUTCMinutes() % 15 === 0;
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function iso(ts = Date.now()) { return new Date(ts).toISOString(); }
function safeNum(v, d = 0) { const n = Number(v); return Number.isFinite(n) ? n : d; }
function bestRealtimePx({ bid, ask, last }) {
  const b = Number(bid);
  const a = Number(ask);
  const l = Number(last);
  if (Number.isFinite(b) && Number.isFinite(a) && b > 0 && a > 0) return (b + a) / 2;
  if (Number.isFinite(l) && l > 0) return l;
  if (Number.isFinite(a) && a > 0) return a;
  if (Number.isFinite(b) && b > 0) return b;
  return NaN;
}
function bestPxFromDepthLevels(bids, asks) {
  const bestBid = Array.isArray(bids) && bids.length ? Number(Array.isArray(bids[0]) ? bids[0][0] : bids[0]?.price) : NaN;
  const bestAsk = Array.isArray(asks) && asks.length ? Number(Array.isArray(asks[0]) ? asks[0][0] : asks[0]?.price) : NaN;
  return bestRealtimePx({ bid: bestBid, ask: bestAsk, last: NaN });
}
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function ensureParent(p) { fs.mkdirSync(path.dirname(p), { recursive: true }); }
function writeJson(filePath, obj) { ensureParent(filePath); fs.writeFileSync(filePath, JSON.stringify(obj, null, 2), 'utf8'); }
function appendJsonl(filePath, obj) { ensureParent(filePath); fs.appendFileSync(filePath, JSON.stringify(obj) + '\n', 'utf8'); }
function parseMaybeJson(v) {
  if (typeof v !== 'string') return v;
  const s = v.trim();
  if ((s.startsWith('[') && s.endsWith(']')) || (s.startsWith('{') && s.endsWith('}'))) {
    try { return JSON.parse(s); } catch { return v; }
  }
  return v;
}
function signDir(v, deadband = 0) {
  const n = Number(v);
  if (!Number.isFinite(n)) return null;
  if (n > deadband) return 'UP';
  if (n < -deadband) return 'DOWN';
  return 'FLAT';
}
function currentRoundStartTs(roundMs = ROUND_MS, ts = Date.now()) {
  return ts - (ts % roundMs);
}
function polymarketSlugForSymbol(symbol) {
  const map = {
    BTC: 'btc',
    ETH: 'eth',
    SOL: 'sol',
    XRP: 'xrp',
    DOGE: 'doge',
    HYPE: 'hype',
    BNB: 'bnb',
  };
  return map[symbol] || symbol.toLowerCase();
}
function buildPolymarketIntervalSlugs(symbol, ts = Date.now()) {
  const base = polymarketSlugForSymbol(symbol);
  const start15 = currentRoundStartTs(15 * 60 * 1000, ts) / 1000;
  const start5 = currentRoundStartTs(5 * 60 * 1000, ts) / 1000;
  return [
    `${base}-updown-15m-${start15}`,
    `${base}-up-or-down-15m-${start15}`,
    `${base}-updown-5m-${start5}`,
    `${base}-up-or-down-5m-${start5}`,
  ];
}
function isOppositeDirection(actual, desired) {
  return !!actual && actual !== 'FLAT' && actual !== desired;
}
function isSupportiveDirection(actual, desired) {
  return !actual || actual === 'FLAT' || actual === desired;
}
function roundKeyForTs(ts) { return Math.floor(ts / ROUND_MS); }
function roundStartMs(key) { return key * ROUND_MS; }
function roundEndMs(key) { return (key + 1) * ROUND_MS; }
function percentileFromSorted(sorted, p) {
  if (!Array.isArray(sorted) || !sorted.length) return null;
  const idx = Math.max(0, Math.min(sorted.length - 1, Math.round((sorted.length - 1) * p)));
  return sorted[idx];
}

function parseArgs(argv) {
  const args = {
    duration: 600,
    reportInterval: 600,
    initialBalance: 7,
    stake: 1,
    minStake: 0.5,
    maxConcurrentTrades: 1,
    oneTradePerRound: true,
    symbols: ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'HYPE', 'BNB'],
    signalWindowMs: 2000,
    historyWindowMs: 5 * 60 * 1000,
    focusWindowMinutes: 15,
    marketLookaheadMinutes: 5,
    minSourcesAgree: 2,
    minPolyLag: 0.04,
    minRoundMoveBps: 2.0,
    minEstimatedWinProb: 0.62,
    maxFlipsPerRound: 3,
    minTradeAgeSeconds: 60,
    minRoundQuality: 5.0,
    minSecondsSinceLastFlip: 90,
    minEntryWindowMinutes: 2,
    maxSourceSpreadBps: 4.5,
    outputJson: '/root/.openclaw/workspace/data/polymarket_crypto_paperbot_multi_summary.json',
    ledgerJsonl: '/root/.openclaw/workspace/data/polymarket_crypto_paperbot_multi_ledger.jsonl',
    statusJson: '/root/.openclaw/workspace/data/polymarket_crypto_paperbot_multi_status.json',
    roundLogJsonl: '/root/.openclaw/workspace/data/polymarket_crypto_paperbot_multi_rounds.jsonl',
    flipLogJsonl: '/root/.openclaw/workspace/data/polymarket_crypto_paperbot_multi_flips.jsonl',
    signalLogJsonl: '/root/.openclaw/workspace/data/polymarket_crypto_paperbot_multi_signals.jsonl',
  };

  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    if (a === '--duration') args.duration = Number(next());
    else if (a === '--report-interval') args.reportInterval = Number(next());
    else if (a === '--initial-balance') args.initialBalance = Number(next());
    else if (a === '--stake') args.stake = Number(next());
    else if (a === '--min-stake') args.minStake = Number(next());
    else if (a === '--max-concurrent-trades') args.maxConcurrentTrades = Number(next());
    else if (a === '--one-trade-per-round') args.oneTradePerRound = String(next()).toLowerCase() !== 'false';
    else if (a === '--symbols') args.symbols = next().split(',').map((x) => x.trim().toUpperCase()).filter(Boolean);
    else if (a === '--signal-window-ms') args.signalWindowMs = Number(next());
    else if (a === '--history-window-ms') args.historyWindowMs = Number(next());
    else if (a === '--focus-window-minutes') args.focusWindowMinutes = Number(next());
    else if (a === '--market-lookahead-minutes') args.marketLookaheadMinutes = Number(next());
    else if (a === '--min-sources-agree') args.minSourcesAgree = Number(next());
    else if (a === '--min-poly-lag') args.minPolyLag = Number(next());
    else if (a === '--min-round-move-bps') args.minRoundMoveBps = Number(next());
    else if (a === '--min-estimated-win-prob') args.minEstimatedWinProb = Number(next());
    else if (a === '--max-flips-per-round') args.maxFlipsPerRound = Number(next());
    else if (a === '--min-trade-age-seconds') args.minTradeAgeSeconds = Number(next());
    else if (a === '--min-round-quality') args.minRoundQuality = Number(next());
    else if (a === '--min-seconds-since-last-flip') args.minSecondsSinceLastFlip = Number(next());
    else if (a === '--min-entry-window-minutes') args.minEntryWindowMinutes = Number(next());
    else if (a === '--max-source-spread-bps') args.maxSourceSpreadBps = Number(next());
    else if (a === '--output-json') args.outputJson = next();
    else if (a === '--ledger-jsonl') args.ledgerJsonl = next();
    else if (a === '--status-json') args.statusJson = next();
    else if (a === '--round-log-jsonl') args.roundLogJsonl = next();
    else if (a === '--flip-log-jsonl') args.flipLogJsonl = next();
    else if (a === '--signal-log-jsonl') args.signalLogJsonl = next();
  }

  args.symbols = args.symbols.filter((sym) => SYMBOLS[sym]);
  if (!args.symbols.length) throw new Error('No valid symbols selected');
  return args;
}

async function fetchJson(url) {
  const res = await fetch(url, { headers: { 'user-agent': USER_AGENT, accept: 'application/json' } });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return await res.json();
}

async function findLiveRoundMarkets(args) {
  const now = Date.now();
  const maxMs = (args.focusWindowMinutes + args.marketLookaheadMinutes) * 60 * 1000;
  const liveBySymbol = new Map(args.symbols.map((sym) => [sym, []]));
  const seenMarketIds = new Set();

  function pushCandidate(sym, m) {
    if (!m || !sym || !liveBySymbol.has(sym)) return;
    const ev = (m.events && m.events[0]) || {};
    const endDate = m.endDate || ev.endDate || null;
    const endTs = endDate ? Date.parse(endDate) : NaN;
    if (!Number.isFinite(endTs)) return;
    const msToEnd = endTs - now;
    if (msToEnd <= 15_000 || msToEnd > maxMs) return;

    const tokenIds = parseMaybeJson(m.clobTokenIds);
    const outcomes = parseMaybeJson(m.outcomes);
    let yesToken = null;
    let noToken = null;
    if (Array.isArray(tokenIds) && Array.isArray(outcomes)) {
      for (let i = 0; i < outcomes.length; i++) {
        const label = String(outcomes[i]).trim().toLowerCase();
        if (label === 'yes' && i < tokenIds.length) yesToken = String(tokenIds[i]);
        if (label === 'no' && i < tokenIds.length) noToken = String(tokenIds[i]);
      }
      if (!yesToken && tokenIds.length) yesToken = String(tokenIds[0]);
      if (!noToken && tokenIds.length > 1) noToken = String(tokenIds[1]);
    }
    if (!yesToken || !noToken) return;

    const marketId = String(m.id);
    if (seenMarketIds.has(marketId)) return;
    seenMarketIds.add(marketId);

    liveBySymbol.get(sym).push({
      symbol: sym,
      marketId,
      eventId: String(ev.id || ''),
      question: String(m.question || m.slug || m.id),
      eventTitle: String(ev.title || ev.slug || ''),
      slug: String(m.slug || ''),
      endDate,
      endTs,
      msToEnd,
      liquidity: safeNum(m.liquidityNum ?? m.liquidity, 0),
      volume24hr: safeNum(m.volume24hr, 0),
      yesTokenId: yesToken,
      noTokenId: noToken,
      roundKey: roundKeyForTs(endTs - 1),
    });
  }

  for (const sym of args.symbols) {
    const slugCandidates = buildPolymarketIntervalSlugs(sym, now);
    for (const slug of slugCandidates) {
      try {
        const data = await fetchJson(`${GAMMA_BASE}/markets?slug=${encodeURIComponent(slug)}`);
        if (Array.isArray(data) && data.length) pushCandidate(sym, data[0]);
      } catch {}
      await sleep(20);
    }
  }

  const arr = [];
  for (let offset = 0; offset < 15000; offset += 500) {
    const data = await fetchJson(`${GAMMA_BASE}/markets?active=true&closed=false&limit=500&offset=${offset}`);
    if (!Array.isArray(data) || !data.length) break;
    arr.push(...data);
    if (data.length < 500) break;
    await sleep(25);
  }

  for (const m of arr) {
    const ev = (m.events && m.events[0]) || {};
    const text = [m.question, m.slug, m.description, ev.title, ev.slug].filter(Boolean).join(' | ');
    if (!isDirectionalShortHorizonMarket(text)) continue;

    const sym = args.symbols.find((s) => SYMBOLS[s] && SYMBOLS[s].match.test(text));
    if (!sym) continue;

    const timeBucketOk = isQuarterHourAligned(Date.parse(m.endDate || ev.endDate || ''))
      || /15\s*min|15\s*minute|15m|quarter\s*hour|:00|:15|:30|:45/i.test(text);
    if (!timeBucketOk) continue;

    pushCandidate(sym, m);
  }

  const chosen = [];
  for (const sym of args.symbols) {
    const xs = liveBySymbol.get(sym) || [];
    xs.sort((a, b) => {
      const aBucket = Math.abs((a.endTs - now) - args.focusWindowMinutes * 60 * 1000);
      const bBucket = Math.abs((b.endTs - now) - args.focusWindowMinutes * 60 * 1000);
      return aBucket - bBucket || (b.volume24hr - a.volume24hr) || (b.liquidity - a.liquidity);
    });
    if (xs.length) chosen.push(xs[0]);
  }
  return chosen;
}

class PolyBook {
  constructor() {
    this.books = new Map();
  }
  setBook(tokenId, asks, bids, tsMs) {
    this.books.set(tokenId, {
      asks: (asks || []).map((x) => ({ price: safeNum(x.price, NaN), size: safeNum(x.size, 0) }))
        .filter((x) => Number.isFinite(x.price) && x.size > 0)
        .sort((a, b) => a.price - b.price),
      bids: (bids || []).map((x) => ({ price: safeNum(x.price, NaN), size: safeNum(x.size, 0) }))
        .filter((x) => Number.isFinite(x.price) && x.size > 0)
        .sort((a, b) => b.price - a.price),
      tsMs: tsMs || Date.now(),
    });
  }
  updateFromChanges(tokenId, changes, tsMs) {
    let book = this.books.get(tokenId);
    if (!book) {
      book = { asks: [], bids: [], tsMs: tsMs || Date.now() };
      this.books.set(tokenId, book);
    }
    const sideMap = {
      BUY: new Map(book.bids.map((x) => [x.price, x.size])),
      SELL: new Map(book.asks.map((x) => [x.price, x.size])),
    };
    for (const ch of changes || []) {
      const side = String(ch.side || '').toUpperCase();
      const px = safeNum(ch.price, NaN);
      const sz = safeNum(ch.size, NaN);
      if (!Number.isFinite(px) || !Number.isFinite(sz) || !sideMap[side]) continue;
      if (sz <= 0) sideMap[side].delete(px);
      else sideMap[side].set(px, sz);
    }
    book.bids = [...sideMap.BUY.entries()].map(([price, size]) => ({ price, size })).sort((a, b) => b.price - a.price);
    book.asks = [...sideMap.SELL.entries()].map(([price, size]) => ({ price, size })).sort((a, b) => a.price - b.price);
    book.tsMs = tsMs || Date.now();
  }
  bestAsk(tokenId) {
    const b = this.books.get(tokenId);
    return b && b.asks.length ? b.asks[0].price : null;
  }
}

class PaperLedger {
  constructor(balance, ledgerPath) {
    this.balance = balance;
    this.realizedPnl = 0;
    this.open = [];
    this.closed = [];
    this.ledgerPath = ledgerPath;
    ensureParent(ledgerPath);
  }
  write(obj) { appendJsonl(this.ledgerPath, obj); }
  canOpen(stake) { return this.balance >= stake; }
  openTrade(trade) {
    this.balance -= trade.stake;
    this.open.push(trade);
    this.write({ type: 'open', ts: iso(), ...trade, balance: +this.balance.toFixed(4), realizedPnl: +this.realizedPnl.toFixed(4) });
  }
  settleTrade(trade, won, meta = {}) {
    const payout = won ? (1 * trade.shares) : 0;
    const pnl = payout - trade.stake;
    this.balance += payout;
    this.realizedPnl += pnl;
    this.closed.push({ ...trade, won, payout, pnl, settledAt: iso(), ...meta });
    this.write({
      type: 'close',
      ts: iso(),
      marketId: trade.marketId,
      symbol: trade.symbol,
      side: trade.side,
      won,
      payout: +payout.toFixed(6),
      pnl: +pnl.toFixed(6),
      balance: +this.balance.toFixed(4),
      realizedPnl: +this.realizedPnl.toFixed(4),
      ...meta,
    });
  }
  stats() {
    const wins = this.closed.filter((x) => x.won).length;
    return {
      balance: +this.balance.toFixed(4),
      openTrades: this.open.length,
      closedTrades: this.closed.length,
      wins,
      winRate: this.closed.length ? +(wins / this.closed.length).toFixed(4) : 0,
      realizedPnl: +this.realizedPnl.toFixed(4),
    };
  }
}

class LeaderCompositeTracker {
  constructor(symbols, freshnessMs, historyWindowMs) {
    this.freshnessMs = freshnessMs;
    this.historyWindowMs = historyWindowMs;
    this.symbols = new Map(symbols.map((sym) => [sym, { sources: new Map(), history: [] }]));
  }
  freshSources(symbol, names = PRIMARY_SOURCES, maxAgeMs = this.freshnessMs) {
    const st = this.symbols.get(symbol);
    if (!st) return [];
    const now = Date.now();
    const out = [];
    for (const name of names) {
      const rec = st.sources.get(name);
      if (!rec) continue;
      if ((now - rec.ts) > maxAgeMs) continue;
      if (!Number.isFinite(rec.price)) continue;
      out.push({ name, price: rec.price, ts: rec.ts });
    }
    return out;
  }
  composite(symbol, names = PRIMARY_SOURCES, maxAgeMs = this.freshnessMs) {
    const fresh = this.freshSources(symbol, names, maxAgeMs);
    if (!fresh.length) return null;
    let weightedSum = 0;
    let totalWeight = 0;
    for (const x of fresh) {
      const w = SOURCE_WEIGHTS[x.name] || 1;
      weightedSum += x.price * w;
      totalWeight += w;
    }
    const price = weightedSum / Math.max(totalWeight, 1e-9);
    const pxs = fresh.map((x) => x.price);
    const minPx = Math.min(...pxs);
    const maxPx = Math.max(...pxs);
    const spreadBps = price ? ((maxPx - minPx) / price) * 10000 : 0;
    return {
      symbol,
      price: +price.toFixed(8),
      sourceCount: fresh.length,
      spreadBps: +spreadBps.toFixed(4),
      sources: fresh,
      ts: Math.max(...fresh.map((x) => x.ts)),
    };
  }
  update(symbol, source, price, ts = Date.now()) {
    const st = this.symbols.get(symbol);
    if (!st || !Number.isFinite(price)) return null;
    st.sources.set(source, { price, ts });
    const comp = this.composite(symbol);
    if (!comp) return null;
    const history = st.history;
    const last = history[history.length - 1];
    if (!last || (comp.ts - last.ts) >= 250) {
      history.push({ ts: comp.ts, price: comp.price, spreadBps: comp.spreadBps, sourceCount: comp.sourceCount });
    } else {
      last.ts = comp.ts;
      last.price = comp.price;
      last.spreadBps = comp.spreadBps;
      last.sourceCount = comp.sourceCount;
    }
    const cutoff = Date.now() - this.historyWindowMs;
    while (history.length && history[0].ts < cutoff) history.shift();
    return comp;
  }
  momentum(symbol, windowMs) {
    const st = this.symbols.get(symbol);
    const cur = this.composite(symbol);
    if (!st || !cur || !st.history.length) return null;
    const cutoff = cur.ts - windowMs;
    let ref = null;
    for (const pt of st.history) {
      if (pt.ts >= cutoff) { ref = pt; break; }
      ref = pt;
    }
    if (!ref || !Number.isFinite(ref.price) || !ref.price) return null;
    const moveBps = ((cur.price - ref.price) / ref.price) * 10000;
    return {
      windowMs,
      fromTs: ref.ts,
      toTs: cur.ts,
      fromPrice: ref.price,
      toPrice: cur.price,
      moveBps: +moveBps.toFixed(4),
    };
  }
  sourceMovesVs(symbol, refPrice, names = PRIMARY_SOURCES, maxAgeMs = this.freshnessMs) {
    if (!Number.isFinite(refPrice) || !refPrice) return [];
    return this.freshSources(symbol, names, maxAgeMs).map((x) => ({
      name: x.name,
      price: x.price,
      ts: x.ts,
      moveBps: +(((x.price - refPrice) / refPrice) * 10000).toFixed(4),
    }));
  }
  snapshot(symbol) {
    return {
      composite: this.composite(symbol),
      mom5s: this.momentum(symbol, 5000),
      mom15s: this.momentum(symbol, 15000),
      mom60s: this.momentum(symbol, 60000),
    };
  }
}

class RoundTracker {
  constructor(symbols, roundMs, roundLogJsonl, flipLogJsonl) {
    this.roundMs = roundMs;
    this.deadbandBps = 0.8;
    this.roundLogJsonl = roundLogJsonl;
    this.flipLogJsonl = flipLogJsonl;
    ensureParent(roundLogJsonl);
    ensureParent(flipLogJsonl);
    this.symbols = new Map(symbols.map((sym) => [sym, { current: null, completed: new Map(), latestCompleted: null }]));
  }
  toSummary(symbol, cur, finalizedReason = null) {
    if (!cur) return null;
    const netMoveBps = cur.openPx ? ((cur.closePx - cur.openPx) / cur.openPx) * 10000 : 0;
    const grossRangeBps = cur.openPx ? ((cur.highPx - cur.lowPx) / cur.openPx) * 10000 : 0;
    const absMoves = cur.samples.map((s) => Math.abs(s.moveBps)).sort((a, b) => a - b);
    const sourceCountArr = cur.samples.map((s) => s.sourceCount || 0);
    const spreadArr = cur.samples.map((s) => s.spreadBps).filter((x) => Number.isFinite(x));
    const largestFlipGapSec = cur.flips.length > 1
      ? Math.max(...cur.flips.slice(1).map((f, i) => (Date.parse(f.ts) - Date.parse(cur.flips[i].ts)) / 1000))
      : null;
    const avgSampleMs = cur.samples.length > 1 ? (cur.lastSeenTs - cur.firstSeenTs) / (cur.samples.length - 1) : null;
    return {
      symbol,
      roundKey: cur.key,
      roundStartTs: iso(cur.roundStartTs),
      roundEndTs: iso(cur.roundEndTs),
      firstSeenTs: iso(cur.firstSeenTs),
      lastSeenTs: iso(cur.lastSeenTs),
      openPx: +cur.openPx.toFixed(8),
      closePx: +cur.closePx.toFixed(8),
      highPx: +cur.highPx.toFixed(8),
      lowPx: +cur.lowPx.toFixed(8),
      netMoveBps: +netMoveBps.toFixed(4),
      grossRangeBps: +grossRangeBps.toFixed(4),
      flipCount: cur.flips.length,
      flips: cur.flips.map((x) => ({ ...x })),
      flipSecondsFromOpen: cur.flips.map((x) => +((Date.parse(x.ts) - cur.roundStartTs) / 1000).toFixed(1)),
      sampleCount: cur.samples.length,
      sampleCoverageRatio: +Math.min(1, (cur.samples.length * 250) / this.roundMs).toFixed(4),
      avgSampleIntervalMs: avgSampleMs == null ? null : +avgSampleMs.toFixed(1),
      sourceCountMin: sourceCountArr.length ? Math.min(...sourceCountArr) : 0,
      sourceCountMax: sourceCountArr.length ? Math.max(...sourceCountArr) : 0,
      sourceCountAvg: sourceCountArr.length ? +(sourceCountArr.reduce((a, b) => a + b, 0) / sourceCountArr.length).toFixed(4) : 0,
      spreadBpsMin: spreadArr.length ? +Math.min(...spreadArr).toFixed(4) : null,
      spreadBpsMax: spreadArr.length ? +Math.max(...spreadArr).toFixed(4) : null,
      spreadBpsAvg: spreadArr.length ? +(spreadArr.reduce((a, b) => a + b, 0) / spreadArr.length).toFixed(4) : null,
      spreadBpsP90: spreadArr.length ? +percentileFromSorted(spreadArr.slice().sort((a, b) => a - b), 0.9).toFixed(4) : null,
      absMoveBpsP50: absMoves.length ? +percentileFromSorted(absMoves, 0.5).toFixed(4) : null,
      absMoveBpsP90: absMoves.length ? +percentileFromSorted(absMoves, 0.9).toFixed(4) : null,
      largestFlipGapSec: largestFlipGapSec == null ? null : +largestFlipGapSec.toFixed(1),
      secondsFromLastFlipToClose: cur.flips.length ? +((cur.lastSeenTs - Date.parse(cur.flips[cur.flips.length - 1].ts)) / 1000).toFixed(1) : null,
      upTicks: cur.upTicks,
      downTicks: cur.downTicks,
      flatTicks: cur.flatTicks,
      openingDirection: cur.openingDirection,
      closingDirection: cur.lastNonFlatDir,
      ageMs: cur.lastSeenTs - cur.firstSeenTs,
      finalizedReason,
    };
  }
  finalize(symbol, reason) {
    const st = this.symbols.get(symbol);
    if (!st || !st.current) return null;
    const cur = st.current;
    const summary = this.toSummary(symbol, cur, reason);
    st.completed.set(cur.key, summary);
    if (st.completed.size > 8) {
      const oldestKey = [...st.completed.keys()].sort((a, b) => a - b)[0];
      st.completed.delete(oldestKey);
    }
    st.latestCompleted = summary;
    appendJsonl(this.roundLogJsonl, { type: 'round_summary', ts: iso(), ...summary });
    st.current = null;
    return summary;
  }
  ingest(symbol, price, ts = Date.now(), meta = {}) {
    if (!Number.isFinite(price)) return null;
    const st = this.symbols.get(symbol);
    if (!st) return null;
    const key = roundKeyForTs(ts);
    if (!st.current || st.current.key !== key) {
      if (st.current) this.finalize(symbol, 'round_roll');
      st.current = {
        key,
        roundStartTs: roundStartMs(key),
        roundEndTs: roundEndMs(key),
        firstSeenTs: ts,
        lastSeenTs: ts,
        openPx: price,
        closePx: price,
        highPx: price,
        lowPx: price,
        lastNonFlatDir: null,
        openingDirection: null,
        flips: [],
        samples: [{ ts, price, moveBps: 0, spreadBps: safeNum(meta.spreadBps, NaN), sourceCount: safeNum(meta.sourceCount, 0) }],
        upTicks: 0,
        downTicks: 0,
        flatTicks: 1,
      };
      return this.toSummary(symbol, st.current, null);
    }
    const cur = st.current;
    cur.lastSeenTs = ts;
    cur.closePx = price;
    if (price > cur.highPx) cur.highPx = price;
    if (price < cur.lowPx) cur.lowPx = price;

    const moveBps = ((price - cur.openPx) / cur.openPx) * 10000;
    cur.samples.push({ ts, price, moveBps: +moveBps.toFixed(4), spreadBps: safeNum(meta.spreadBps, NaN), sourceCount: safeNum(meta.sourceCount, 0) });
    const dir = signDir(moveBps, this.deadbandBps);
    if (dir === 'UP') cur.upTicks += 1;
    else if (dir === 'DOWN') cur.downTicks += 1;
    else cur.flatTicks += 1;
    if (!cur.openingDirection && dir && dir !== 'FLAT') cur.openingDirection = dir;
    if (dir && dir !== 'FLAT') {
      if (cur.lastNonFlatDir && dir !== cur.lastNonFlatDir) {
        const flip = {
          ts: iso(ts),
          secondsFromRoundOpen: +((ts - cur.roundStartTs) / 1000).toFixed(1),
          secondsFromFirstSeen: +((ts - cur.firstSeenTs) / 1000).toFixed(1),
          dir,
          price: +price.toFixed(8),
          moveBps: +moveBps.toFixed(4),
        };
        cur.flips.push(flip);
        appendJsonl(this.flipLogJsonl, { type: 'round_flip', symbol, roundKey: cur.key, ...flip });
      }
      cur.lastNonFlatDir = dir;
    }
    return this.toSummary(symbol, cur, null);
  }
  expire(ts = Date.now()) {
    for (const sym of this.symbols.keys()) {
      const st = this.symbols.get(sym);
      if (st?.current && ts >= st.current.roundEndTs + 1000) this.finalize(sym, 'time_expire');
    }
  }
  currentSummary(symbol) {
    const st = this.symbols.get(symbol);
    return st?.current ? this.toSummary(symbol, st.current, null) : null;
  }
  latestCompletedSummary(symbol) {
    return this.symbols.get(symbol)?.latestCompleted || null;
  }
  getSummary(symbol, roundKey) {
    const st = this.symbols.get(symbol);
    if (!st) return null;
    if (st.current && st.current.key === roundKey) return this.toSummary(symbol, st.current, null);
    return st.completed.get(roundKey) || null;
  }
  flushOpen(reason = 'script_exit') {
    for (const sym of this.symbols.keys()) {
      const st = this.symbols.get(sym);
      if (st?.current) this.finalize(sym, reason);
    }
  }
}

function estimateWinProb(ctx, args) {
  const profile = strategyProfile(ctx.symbol);
  let p = 0.50;
  p += clamp(Math.abs(ctx.roundMoveBps) / (profile.moveScaleBps * 100), 0, 0.06);
  p += clamp(Math.abs(ctx.mom15Bps || 0) / (profile.momScaleBps * 100), 0, 0.05);
  p += clamp(Math.abs(ctx.mom60Bps || 0) / (profile.momScaleBps * 140), 0, 0.03);
  p += ctx.agreeingSources >= 3 ? 0.04 : (ctx.agreeingSources >= 2 ? 0.022 : (ctx.agreeingSources >= 1 ? 0.006 : 0));
  p += clamp((ctx.edge || 0) / 0.20, -0.12, 0.12);
  p += clamp((ctx.roundQuality - profile.minQuality) / 140, -0.05, 0.05);
  p -= Math.min(0.16, Math.max(0, ctx.flipCount) * profile.flipPenalty);
  p -= Math.min(0.06, Math.max(0, (ctx.sourceSpreadBps || 0) - profile.maxSpreadBps) / 80);
  p -= Math.min(0.05, Math.max(0, (ctx.desiredAsk || 0) - 0.47) / 0.10);
  p += Math.min(0.035, Math.max(0, 0.43 - (ctx.desiredAsk || 0)) / 0.12);
  if (ctx.mom5Contrary) p -= 0.01;
  if (ctx.mom15Contrary) p -= 0.05;
  if (ctx.mom60Contrary) p -= 0.025;
  if ((ctx.secondsSinceLastFlip ?? 9999) < profile.minSecondsSinceLastFlip) p -= 0.05;
  if (ctx.msToEnd <= 4 * 60 * 1000) p += 0.025;
  else if (ctx.msToEnd <= 7 * 60 * 1000) p += 0.012;
  else if (ctx.msToEnd > 11 * 60 * 1000) p -= 0.025;
  if (ctx.roundAgeSec < args.minTradeAgeSeconds) p -= 0.04;
  return clamp(+p.toFixed(4), 0.36, profile.confidenceCap);
}

function evaluateMarket(m, book, leaders, rounds, args) {
  const yesAsk = book.bestAsk(m.yesTokenId);
  const noAsk = book.bestAsk(m.noTokenId);
  if (yesAsk == null || noAsk == null) return null;

  const now = Date.now();
  const msToEnd = m.endTs - now;
  if (msToEnd <= 10_000 || msToEnd > args.focusWindowMinutes * 60 * 1000) return null;
  if (msToEnd < args.minEntryWindowMinutes * 60 * 1000) return null;

  const round = rounds.currentSummary(m.symbol) || rounds.getSummary(m.symbol, m.roundKey);
  const leader = leaders.composite(m.symbol);
  if (!round || !leader) return null;

  const profile = strategyProfile(m.symbol);
  const minSourcesAgree = Math.max(1, Math.min(args.minSourcesAgree, profile.minSourceCount, availableSourceCount(m.symbol)));
  const minRoundMoveBps = Math.max(args.minRoundMoveBps, profile.minRoundMoveBps);
  const roundMoveBps = round.openPx ? (((leader.price - round.openPx) / round.openPx) * 10000) : 0;
  const direction = signDir(roundMoveBps, minRoundMoveBps);
  if (!direction || direction === 'FLAT') return null;

  const fairProb = clamp(0.5 + (roundMoveBps / 28_000) + ((leaders.momentum(m.symbol, 15000)?.moveBps || 0) / 22_000) + ((leaders.momentum(m.symbol, 60000)?.moveBps || 0) / 36_000), 0.08, 0.92);
  const desiredSide = direction === 'UP' ? 'YES' : 'NO';
  const desiredAsk = desiredSide === 'YES' ? yesAsk : noAsk;
  const oppositeAsk = desiredSide === 'YES' ? noAsk : yesAsk;
  if (!Number.isFinite(desiredAsk) || desiredAsk <= 0 || desiredAsk >= 0.97) return null;

  const polyLag = fairProb - desiredAsk;
  const priceGap = Math.abs(oppositeAsk - desiredAsk);
  const srcMoves = leaders.sourceMovesVs(m.symbol, round.openPx);
  const agreeing = srcMoves.filter((x) => direction === 'UP' ? x.moveBps > 0 : x.moveBps < 0);

  const mom5 = leaders.momentum(m.symbol, 5000);
  const mom15 = leaders.momentum(m.symbol, 15000);
  const mom60 = leaders.momentum(m.symbol, 60000);
  const mom5Dir = signDir(mom5?.moveBps, 0.25);
  const mom15Dir = signDir(mom15?.moveBps, 0.5);
  const mom60Dir = signDir(mom60?.moveBps, 0.75);
  const roundAgeSec = Math.max(0, (now - Date.parse(round.roundStartTs)) / 1000);
  const trendStrengthBps = Math.abs(roundMoveBps);
  const roundQuality = trendStrengthBps / (1 + safeNum(round.flipCount, 0));
  const lastFlip = Array.isArray(round.flips) && round.flips.length ? round.flips[round.flips.length - 1] : null;
  const lastFlipTs = lastFlip?.ts ? Date.parse(lastFlip.ts) : NaN;
  const secondsSinceLastFlip = Number.isFinite(lastFlipTs) ? Math.max(0, (now - lastFlipTs) / 1000) : null;
  const sourceCoverage = leader.sourceCount / Math.max(1, availableSourceCount(m.symbol));
  const expectedValue = fairProb - desiredAsk;
  const edge = expectedValue;
  const minutesToEnd = Math.max(msToEnd / 60000, 1 / 6);
  const evPerMinute = expectedValue / minutesToEnd;

  const ctx = {
    symbol: m.symbol,
    marketId: m.marketId,
    question: m.question,
    direction,
    desiredSide,
    desiredAsk: +desiredAsk.toFixed(4),
    oppositeAsk: +oppositeAsk.toFixed(4),
    fairProb: +fairProb.toFixed(4),
    polyLag: +polyLag.toFixed(4),
    edge: +edge.toFixed(4),
    expectedValue: +expectedValue.toFixed(4),
    priceGap: +priceGap.toFixed(4),
    minutesToEnd: +minutesToEnd.toFixed(3),
    evPerMinute: +evPerMinute.toFixed(5),
    msToEnd,
    roundAgeSec: +roundAgeSec.toFixed(1),
    roundMoveBps: +roundMoveBps.toFixed(4),
    flipCount: safeNum(round.flipCount, 0),
    agreeingSources: agreeing.length,
    sourceSpreadBps: leader.spreadBps,
    leaderPrice: leader.price,
    roundOpenPx: round.openPx,
    sourceCoverage: +sourceCoverage.toFixed(4),
    roundQuality: +roundQuality.toFixed(4),
    secondsSinceLastFlip: secondsSinceLastFlip == null ? null : +secondsSinceLastFlip.toFixed(1),
    mom5Bps: mom5?.moveBps ?? null,
    mom15Bps: mom15?.moveBps ?? null,
    mom60Bps: mom60?.moveBps ?? null,
    mom5Aligned: isSupportiveDirection(mom5Dir, direction),
    mom15Aligned: mom15Dir === direction,
    mom60Aligned: isSupportiveDirection(mom60Dir, direction),
    mom5Contrary: isOppositeDirection(mom5Dir, direction),
    mom15Contrary: isOppositeDirection(mom15Dir, direction),
    mom60Contrary: isOppositeDirection(mom60Dir, direction),
    profile,
    minSourcesAgree,
  };

  const blockers = [];
  if (ctx.agreeingSources < minSourcesAgree) blockers.push('not_enough_source_agreement');
  if (ctx.sourceCoverage < (minSourcesAgree / Math.max(1, availableSourceCount(m.symbol)))) blockers.push('insufficient_source_coverage');
  if (ctx.edge < profile.minEdge) blockers.push('edge_too_small');
  if (ctx.polyLag < args.minPolyLag) blockers.push('poly_lag_too_small');
  if (ctx.flipCount > Math.min(args.maxFlipsPerRound, profile.maxFlips)) blockers.push('too_many_flips');
  if (ctx.roundQuality < Math.max(args.minRoundQuality, profile.minQuality)) blockers.push('round_quality_too_low');
  if (ctx.sourceSpreadBps > Math.min(args.maxSourceSpreadBps, profile.maxSpreadBps)) blockers.push('source_spread_too_wide');
  if (ctx.secondsSinceLastFlip != null && ctx.secondsSinceLastFlip < Math.max(args.minSecondsSinceLastFlip, profile.minSecondsSinceLastFlip)) blockers.push('recent_flip_too_close');
  if (ctx.mom15Contrary) blockers.push('mom15_contrary');
  if (ctx.mom60Contrary) blockers.push('mom60_contrary');
  if (!ctx.mom15Aligned) blockers.push('mom15_not_aligned');
  if (ctx.roundAgeSec < args.minTradeAgeSeconds) blockers.push('round_too_young');
  if (ctx.desiredAsk > 0.58) blockers.push('price_too_expensive');

  ctx.estimatedWinProb = estimateWinProb(ctx, args);
  if (ctx.estimatedWinProb < Math.max(args.minEstimatedWinProb, profile.minEstimatedWinProb)) blockers.push('estimated_win_prob_below_threshold');
  if (ctx.evPerMinute < 0.003) blockers.push('ev_per_minute_too_small');
  ctx.score = +(ctx.evPerMinute * 10000 + ctx.expectedValue * 60 + (ctx.estimatedWinProb - 0.5) * 8 + Math.min(ctx.roundQuality, 12) * 0.2 - ctx.flipCount * 0.5).toFixed(4);
  ctx.pass = blockers.length === 0;
  ctx.blockers = blockers;
  return ctx;
}

async function main() {
  const args = parseArgs(process.argv);
  [args.outputJson, args.ledgerJsonl, args.statusJson, args.roundLogJsonl, args.flipLogJsonl, args.signalLogJsonl].forEach(ensureParent);

  let chosen = await findLiveRoundMarkets(args);
  let chosenByMarket = new Map(chosen.map((m) => [m.marketId, m]));

  const book = new PolyBook();
  const ledger = new PaperLedger(args.initialBalance, args.ledgerJsonl);
  const leaders = new LeaderCompositeTracker(args.symbols, args.signalWindowMs, args.historyWindowMs);
  const rounds = new RoundTracker(args.symbols, ROUND_MS, args.roundLogJsonl, args.flipLogJsonl);
  const sockets = [];

  const started = Date.now();
  const deadline = started + args.duration * 1000;
  let lastReport = 0;
  let lastLearningSnapshotKey = null;
  let lastMarketRefreshTs = 0;
  let polySocket = null;

  function currentDecisionBoard() {
    const evals = [];
    for (const m of chosen) {
      const e = evaluateMarket(m, book, leaders, rounds, args);
      if (e) evals.push(e);
    }
    evals.sort((a, b) => b.estimatedWinProb - a.estimatedWinProb);
    return evals;
  }

  function refreshStatus() {
    const decisions = currentDecisionBoard();
    const topDecisionBySymbol = Object.fromEntries(args.symbols.map((sym) => {
      const hit = decisions.find((d) => d.symbol === sym) || null;
      return [sym, hit];
    }));
    const chosenBySymbol = Object.fromEntries(chosen.map((m) => [m.symbol, m]));
    const status = {
      ts: iso(),
      startedAt: iso(started),
      config: {
        symbols: args.symbols,
        primarySources: PRIMARY_SOURCES,
        minSourcesAgree: args.minSourcesAgree,
        minPolyLag: args.minPolyLag,
        minEstimatedWinProb: args.minEstimatedWinProb,
        maxFlipsPerRound: args.maxFlipsPerRound,
        minRoundQuality: args.minRoundQuality,
        minSecondsSinceLastFlip: args.minSecondsSinceLastFlip,
        minEntryWindowMinutes: args.minEntryWindowMinutes,
        maxSourceSpreadBps: args.maxSourceSpreadBps,
      },
      liveMarketsFound: chosen.length,
      chosenMarkets: chosen.map((m) => ({
        symbol: m.symbol,
        marketId: m.marketId,
        question: m.question,
        endDate: m.endDate,
        liquidity: m.liquidity,
        volume24hr: m.volume24hr,
        roundKey: m.roundKey,
      })),
      stats: ledger.stats(),
      leaders: Object.fromEntries(args.symbols.map((sym) => [sym, leaders.snapshot(sym)])),
      rounds: Object.fromEntries(args.symbols.map((sym) => [sym, {
        current: rounds.currentSummary(sym),
        latestCompleted: rounds.latestCompletedSummary(sym),
      }])),
      perSymbolLearning: Object.fromEntries(args.symbols.map((sym) => [sym, {
        market: chosenBySymbol[sym] ? {
          marketId: chosenBySymbol[sym].marketId,
          question: chosenBySymbol[sym].question,
          endDate: chosenBySymbol[sym].endDate,
          liquidity: chosenBySymbol[sym].liquidity,
          volume24hr: chosenBySymbol[sym].volume24hr,
        } : null,
        leader: leaders.snapshot(sym),
        roundCurrent: rounds.currentSummary(sym),
        roundLatestCompleted: rounds.latestCompletedSummary(sym),
        topDecision: topDecisionBySymbol[sym],
      }])),
      decisions: decisions.slice(0, 12),
      notes: chosen.length ? [] : ['No current live 15m Polymarket markets matched the selected symbols; running in log-only mode'],
    };
    writeJson(args.statusJson, status);
    return status;
  }

  function report(reason = 'interval') {
    const status = refreshStatus();
    appendJsonl(args.signalLogJsonl, { type: 'status_report', reason, ts: status.ts, stats: status.stats, topDecisions: status.decisions.slice(0, 6) });
    console.log(JSON.stringify({ type: 'report', reason, ...status }));
  }

  function appendLearningSnapshot(reason = 'interval') {
    const now = Date.now();
    const roundKey = roundKeyForTs(now);
    if (reason === 'interval' && lastLearningSnapshotKey === roundKey) return;
    lastLearningSnapshotKey = roundKey;
    const payload = {
      type: 'learning_snapshot',
      ts: iso(now),
      reason,
      roundKey,
      roundStartTs: iso(roundStartMs(roundKey)),
      roundEndTs: iso(roundEndMs(roundKey)),
      bySymbol: Object.fromEntries(args.symbols.map((sym) => {
        const leader = leaders.snapshot(sym);
        const currentRound = rounds.currentSummary(sym);
        const latestCompleted = rounds.latestCompletedSummary(sym);
        const market = chosen.find((m) => m.symbol === sym) || null;
        const topDecision = currentDecisionBoard().find((d) => d.symbol === sym) || null;
        return [sym, {
          leader,
          currentRound,
          latestCompleted,
          market: market ? {
            marketId: market.marketId,
            question: market.question,
            endDate: market.endDate,
            liquidity: market.liquidity,
            volume24hr: market.volume24hr,
          } : null,
          topDecision,
        }];
      })),
    };
    appendJsonl(args.signalLogJsonl, payload);
  }

  function canOpenForRound(evalCtx) {
    return !ledger.open.some((t) => t.symbol === evalCtx.symbol && t.roundKey === chosenByMarket.get(t.marketId)?.roundKey);
  }

  function calcStakeAmount(d) {
    const profile = strategyProfile(d.symbol);
    const baseCap = ledger.balance * profile.maxStakePct;
    const convictionBoost = clamp((d.estimatedWinProb - 0.5) / 0.20, 0.25, 1.0);
    const edgeBoost = clamp((d.edge || 0) / Math.max(profile.minEdge, 0.001), 0.4, 1.4);
    const raw = Math.min(args.stake, baseCap * convictionBoost * edgeBoost);
    return +Math.max(args.minStake, Math.min(raw, ledger.balance)).toFixed(4);
  }

  function maybeOpenTrades() {
    const decisions = currentDecisionBoard().filter((d) => d.pass).sort((a, b) => b.score - a.score);
    if (!decisions.length) return;
    if (ledger.open.length >= args.maxConcurrentTrades) return;

    const candidates = args.oneTradePerRound ? decisions.slice(0, 1) : decisions;
    for (const d of candidates) {
      const m = chosenByMarket.get(d.marketId);
      if (!m) continue;
      if (ledger.open.some((t) => t.marketId === d.marketId)) continue;
      if (ledger.open.some((t) => t.symbol === d.symbol && t.roundKey === m.roundKey)) continue;
      if (args.oneTradePerRound && ledger.open.some((t) => t.roundKey === m.roundKey)) continue;
      if (ledger.open.length >= args.maxConcurrentTrades) break;

      const stake = calcStakeAmount(d);
      if (stake < args.minStake || !ledger.canOpen(stake)) continue;
      const shares = stake / d.desiredAsk;
      ledger.openTrade({
        symbol: d.symbol,
        marketId: d.marketId,
        eventId: m.eventId,
        roundKey: m.roundKey,
        endTs: m.endTs,
        question: d.question,
        side: d.desiredSide,
        entryPrice: d.desiredAsk,
        shares: +shares.toFixed(6),
        stake: +stake.toFixed(6),
        estimatedWinProb: d.estimatedWinProb,
        expectedValue: d.expectedValue,
        fairProb: d.fairProb,
        edge: d.edge,
        score: d.score,
        rationale: {
          leaderDirection: d.direction,
          agreeingSources: d.agreeingSources,
          roundMoveBps: d.roundMoveBps,
          roundQuality: d.roundQuality,
          sourceCoverage: d.sourceCoverage,
          secondsSinceLastFlip: d.secondsSinceLastFlip,
          mom5Bps: d.mom5Bps,
          mom15Bps: d.mom15Bps,
          mom60Bps: d.mom60Bps,
          flipCount: d.flipCount,
          desiredAsk: d.desiredAsk,
          oppositeAsk: d.oppositeAsk,
          fairProb: d.fairProb,
          expectedValue: d.expectedValue,
          edge: d.edge,
          polyLag: d.polyLag,
          priceGap: d.priceGap,
          sourceSpreadBps: d.sourceSpreadBps,
          roundOpenPx: d.roundOpenPx,
          leaderPrice: d.leaderPrice,
          roundAgeSec: d.roundAgeSec,
          msToEnd: d.msToEnd,
          score: d.score,
          blockers: d.blockers,
        },
        openedAt: iso(),
      });
      appendJsonl(args.signalLogJsonl, { type: 'open_decision', ts: iso(), ...d, stake });
    }
  }

  function maybeSettleTrades() {
    const remain = [];
    for (const t of ledger.open) {
      const currentRound = rounds.currentSummary(t.symbol) || rounds.getSummary(t.symbol, t.roundKey);
      const currentEval = chosenByMarket.get(t.marketId) ? evaluateMarket(chosenByMarket.get(t.marketId), book, leaders, rounds, args) : null;

      if (Date.now() < t.endTs - 30_000) {
        const sideMismatch = currentEval && ((t.side === 'YES' && currentEval.direction === 'DOWN') || (t.side === 'NO' && currentEval.direction === 'UP'));
        const edgeBroken = currentEval && currentEval.edge < -0.01;
        const noisyBreak = currentEval && currentEval.flipCount >= 3 && currentEval.secondsSinceLastFlip != null && currentEval.secondsSinceLastFlip < 45;
        if (sideMismatch || edgeBroken || noisyBreak) {
          ledger.write({ type: 'early_exit_signal', ts: iso(), marketId: t.marketId, symbol: t.symbol, side: t.side, currentEval, reason: sideMismatch ? 'direction_reversed' : edgeBroken ? 'edge_broken' : 'noise_spike' });
        }
        remain.push(t);
        continue;
      }

      if (Date.now() < t.endTs + 5000) { remain.push(t); continue; }
      const roundSummary = rounds.getSummary(t.symbol, t.roundKey);
      if (!roundSummary || !Number.isFinite(roundSummary.openPx) || !Number.isFinite(roundSummary.closePx)) { remain.push(t); continue; }
      const won = t.side === 'YES' ? (roundSummary.closePx > roundSummary.openPx) : (roundSummary.closePx < roundSummary.openPx);
      ledger.settleTrade(t, won, {
        roundKey: t.roundKey,
        settleOpenPx: roundSummary.openPx,
        settleClosePx: roundSummary.closePx,
        settleFlipCount: roundSummary.flipCount,
      });
      appendJsonl(args.signalLogJsonl, { type: 'settle_decision', ts: iso(), marketId: t.marketId, symbol: t.symbol, won, roundSummary });
    }
    ledger.open = remain;
  }

  function openPrimarySockets() {
    for (const sym of args.symbols) {
      const cfg = SYMBOLS[sym];
      if (!cfg || !cfg.sources) continue;
      if (cfg.sources.binance?.url) {
        const ws = new WebSocket(cfg.sources.binance.url);
        ws.addEventListener('message', (ev) => {
          try {
            const m = JSON.parse(typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8'));
            const px = bestPxFromDepthLevels(m.b, m.a);
            const ts = safeNum(m.E, Date.now());
            const comp = leaders.update(sym, 'binance', px, ts);
            if (comp) rounds.ingest(sym, comp.price, comp.ts, { sourceCount: comp.sourceCount, spreadBps: comp.spreadBps });
          } catch {}
        });
        sockets.push(ws);
      }
      if (cfg.sources.coinbase?.url && cfg.sources.coinbase?.product) {
        const ws = new WebSocket(cfg.sources.coinbase.url);
        ws.addEventListener('open', () => ws.send(JSON.stringify({ type: 'subscribe', channel: 'ticker', product_ids: [cfg.sources.coinbase.product] })));
        ws.addEventListener('message', (ev) => {
          try {
            const m = JSON.parse(typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8'));
            const px = safeNum(m?.events?.[0]?.tickers?.[0]?.price, NaN);
            const comp = leaders.update(sym, 'coinbase', px, Date.now());
            if (comp) rounds.ingest(sym, comp.price, comp.ts, { sourceCount: comp.sourceCount, spreadBps: comp.spreadBps });
          } catch {}
        });
        sockets.push(ws);
      }
      if (cfg.sources.okx?.url && cfg.sources.okx?.instId) {
        const ws = new WebSocket(cfg.sources.okx.url);
        ws.addEventListener('open', () => ws.send(JSON.stringify({ op: 'subscribe', args: [{ channel: 'tickers', instId: cfg.sources.okx.instId }] })));
        ws.addEventListener('message', (ev) => {
          try {
            const m = JSON.parse(typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8'));
            const t = m?.data?.[0] || {};
            const px = bestPxFromDepthLevels(t.bids, t.asks) || bestRealtimePx({ bid: t.bidPx, ask: t.askPx, last: t.last });
            const comp = leaders.update(sym, 'okx', px, Date.now());
            if (comp) rounds.ingest(sym, comp.price, comp.ts, { sourceCount: comp.sourceCount, spreadBps: comp.spreadBps });
          } catch {}
        });
        sockets.push(ws);
      }
    }
  }

  function openPolySocket() {
    if (!chosen.length) return;
    if (polySocket) {
      try { polySocket.close(); } catch {}
    }
    const wsPoly = new WebSocket(POLY_WS);
    polySocket = wsPoly;
    wsPoly.addEventListener('open', () => {
      const tokenIds = [];
      for (const m of chosen) tokenIds.push(m.yesTokenId, m.noTokenId);
      wsPoly.send(JSON.stringify({ assets_ids: tokenIds, type: 'market', initial_dump: true, level: 2, custom_feature_enabled: true }));
    });
    wsPoly.addEventListener('message', (ev) => {
      const raw = typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8');
      if (raw === 'PONG') return;
      let msg;
      try { msg = JSON.parse(raw); } catch { return; }
      const handle = (x) => {
        if (!x || typeof x !== 'object') return;
        if (x.event_type === 'book') {
          const tokenId = String(x.asset_id || x.token_id || '');
          if (tokenId) book.setBook(tokenId, x.asks || [], x.bids || [], safeNum(x.timestamp, Date.now()));
        } else if (x.event_type === 'price_change') {
          const changes = x.price_changes || [];
          const tokenId = changes[0] ? String(changes[0].asset_id || '') : '';
          if (tokenId) book.updateFromChanges(tokenId, changes, safeNum(x.timestamp, Date.now()));
        }
      };
      if (Array.isArray(msg)) msg.forEach(handle);
      else handle(msg);
    });
    sockets.push(wsPoly);
  }

  async function refreshLiveMarkets(force = false) {
    const now = Date.now();
    if (!force && (now - lastMarketRefreshTs) < 60_000) return;
    lastMarketRefreshTs = now;
    const nextChosen = await findLiveRoundMarkets(args);
    const nextIds = nextChosen.map((m) => m.marketId).sort().join(',');
    const prevIds = chosen.map((m) => m.marketId).sort().join(',');
    chosen = nextChosen;
    chosenByMarket = new Map(chosen.map((m) => [m.marketId, m]));
    if (force || nextIds !== prevIds) openPolySocket();
  }

  openPrimarySockets();
  await refreshLiveMarkets(true);

  report('startup');
  appendLearningSnapshot('startup');
  lastReport = Date.now();

  while (Date.now() < deadline) {
    rounds.expire();
    await refreshLiveMarkets(false);
    if (chosen.length) {
      maybeOpenTrades();
      maybeSettleTrades();
    }
    appendLearningSnapshot('interval');
    if ((Date.now() - lastReport) >= args.reportInterval * 1000) {
      report('interval');
      lastReport = Date.now();
    }
    await sleep(500);
  }

  rounds.expire();
  rounds.flushOpen('script_exit');
  maybeSettleTrades();
  appendLearningSnapshot('final');
  report('final');

  const out = refreshStatus();
  writeJson(args.outputJson, out);
  for (const ws of sockets) { try { ws.close(); } catch {} }
}

main().catch((err) => {
  console.error(err && err.stack || err);
  process.exit(1);
});
