#!/usr/bin/env node

const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 5011;
const DATA_DIR = '/root/.openclaw/workspace/data';
const GAMMA_BASE = 'https://gamma-api.polymarket.com';
const POLY_WS = 'wss://ws-subscriptions-clob.polymarket.com/ws/market';
const USER_AGENT = 'openclaw-polymarket-dashboard/0.3';
const DISPLAY_TZ = 'Asia/Shanghai';
const PAPER_PLAN = {
  totalBudget: 10,
  todayCostApplied: 3,
  dailyRunRate: 3,
};

const SYMBOLS = {
  BTC: { label: 'Bitcoin', re: /(bitcoin|\bbtc\b)/i, binance: 'btcusdt', coinbase: 'BTC-USD', okx: 'BTC-USDT', bybit: 'BTCUSDT', kraken: 'BTC/USD' },
  ETH: { label: 'Ethereum', re: /(ethereum|\beth\b)/i, binance: 'ethusdt', coinbase: 'ETH-USD', okx: 'ETH-USDT', bybit: 'ETHUSDT', kraken: 'ETH/USD' },
  SOL: { label: 'Solana', re: /(solana|\bsol\b)/i, binance: 'solusdt', coinbase: 'SOL-USD', okx: 'SOL-USDT', bybit: 'SOLUSDT', kraken: 'SOL/USD' },
  XRP: { label: 'XRP', re: /(\bxrp\b|ripple)/i, binance: 'xrpusdt', coinbase: 'XRP-USD', okx: 'XRP-USDT', bybit: 'XRPUSDT', kraken: 'XRP/USD' },
  DOGE: { label: 'Dogecoin', re: /(dogecoin|\bdoge\b)/i, binance: 'dogeusdt', coinbase: 'DOGE-USD', okx: 'DOGE-USDT', bybit: 'DOGEUSDT', kraken: 'DOGE/USD' },
  HYPE: { label: 'Hyperliquid', re: /(hyperliquid|\bhype\b)/i, binance: null, coinbase: null, okx: null, bybit: null, kraken: null },
  BNB: { label: 'BNB', re: /(binance coin|\bbnb\b)/i, binance: 'bnbusdt', coinbase: null, okx: 'BNB-USDT', bybit: 'BNBUSDT', kraken: null },
};

const state = {
  startedAt: new Date().toISOString(),
  lastRefreshAt: null,
  markets: {},
  sourcePrices: {},
  recommendations: {},
  notes: [],
  paper: {
    budget: {
      totalBudget: PAPER_PLAN.totalBudget,
      todayCostApplied: PAPER_PLAN.todayCostApplied,
      dailyRunRate: PAPER_PLAN.dailyRunRate,
      netBudget: +(PAPER_PLAN.totalBudget - PAPER_PLAN.todayCostApplied).toFixed(2),
    },
    latestSummaryFile: null,
    latestSummaryTs: null,
    latestStatusFile: null,
    latestStatusMtime: null,
    latestStatus: null,
    botOnline: false,
    botMode: 'unknown',
    botLagSec: null,
    stats: null,
    chosenMarkets: [],
    tradeLogFile: null,
    tradeLogMatched: false,
    tradeLogNote: 'No paper state loaded yet',
    signalsFile: null,
    trades: [],
    activity: [],
    blockerCounts: [],
    perSymbolLearning: {},
    decisions: [],
  },
};

function safeNum(v, d = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

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

function parseMaybeJson(v) {
  if (typeof v !== 'string') return v;
  const s = v.trim();
  if ((s.startsWith('[') && s.endsWith(']')) || (s.startsWith('{') && s.endsWith('}'))) {
    try { return JSON.parse(s); } catch { return v; }
  }
  return v;
}

function esc(v) {
  return String(v ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtDollar(v) {
  const n = Number(v);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : '—';
}

function fmtSignedDollar(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return `${n >= 0 ? '+' : '-'}$${Math.abs(n).toFixed(2)}`;
}

function fmtPct(v) {
  const n = Number(v);
  return Number.isFinite(n) ? `${n.toFixed(1)}%` : '—';
}

function fmtBps(v, digits = 1) {
  const n = Number(v);
  return Number.isFinite(n) ? `${n.toFixed(digits)} bps` : '—';
}

function fmtSec(v) {
  const n = Number(v);
  return Number.isFinite(n) ? `${n.toFixed(n >= 100 ? 0 : 1)}s` : '—';
}

function formatInTz(v, opts = {}) {
  if (!v || v === '—') return '—';
  const d = new Date(v);
  if (!Number.isFinite(d.getTime())) return String(v);
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: DISPLAY_TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    ...opts,
  }).format(d);
}

function fmtTs(v) {
  if (!v) return '—';
  return formatInTz(v, { year: undefined, month: undefined, day: undefined }).replace(',', '');
}

function fmtDate(v) {
  if (!v) return '—';
  return formatInTz(v).replace(',', '');
}

function toneClass(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 'muted';
  if (n > 0) return 'good';
  if (n < 0) return 'bad';
  return 'warn';
}

function pillClass(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 'pill-muted';
  if (n > 0) return 'pill-good';
  if (n < 0) return 'pill-bad';
  return 'pill-warn';
}

function symbolFromText(text) {
  const s = String(text || '');
  for (const [sym, cfg] of Object.entries(SYMBOLS)) {
    if (cfg.re.test(s)) return sym;
  }
  return null;
}

const BLOCKER_LABELS = {
  not_enough_source_agreement: '多源一致性不足',
  insufficient_source_coverage: '来源覆盖不足',
  edge_too_small: '优势不足',
  poly_lag_too_small: 'Polymarket 滞后太小',
  too_many_flips: '翻转次数过多',
  round_quality_too_low: '本轮质量过低',
  source_spread_too_wide: '多源价差过大',
  recent_flip_too_close: '最近翻转离现在太近',
  mom15_contrary: '15秒动量反向',
  mom60_contrary: '60秒动量反向',
  mom15_not_aligned: '15秒动量未对齐',
  round_too_young: '本轮太早',
  price_too_expensive: '价格过贵',
  estimated_win_prob_below_threshold: '估算胜率低于门槛',
  ev_per_minute_too_small: '单位时间期望收益过小',
};

const BLOCKER_SEVERITY = {
  estimated_win_prob_below_threshold: 100,
  too_many_flips: 95,
  round_quality_too_low: 90,
  recent_flip_too_close: 85,
  source_spread_too_wide: 80,
  not_enough_source_agreement: 78,
  insufficient_source_coverage: 76,
  mom15_contrary: 72,
  mom60_contrary: 68,
  mom15_not_aligned: 64,
  poly_lag_too_small: 58,
  edge_too_small: 54,
  round_too_young: 50,
  price_too_expensive: 46,
  ev_per_minute_too_small: 42,
};

function blockerLabel(x) {
  return BLOCKER_LABELS[x] || x;
}

function orderedBlockers(xs) {
  return (xs || []).slice().sort((a, b) => (BLOCKER_SEVERITY[b] || 0) - (BLOCKER_SEVERITY[a] || 0));
}

function minsText(ms) {
  const n = Number(ms);
  return Number.isFinite(n) ? `${Math.round(n / 60000)}m` : '—';
}

function agoSeconds(ts) {
  const x = Date.parse(ts || '');
  if (!Number.isFinite(x)) return null;
  return Math.max(0, (Date.now() - x) / 1000);
}

function logNote(msg) {
  state.notes.unshift({ ts: new Date().toISOString(), msg });
  state.notes = state.notes.slice(0, 50);
}

async function fetchJson(url) {
  const res = await fetch(url, {
    headers: { 'user-agent': USER_AGENT, accept: 'application/json' },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return await res.json();
}

async function loadMarkets() {
  const all = [];
  for (let offset = 0; offset < 15000; offset += 500) {
    const arr = await fetchJson(`${GAMMA_BASE}/markets?closed=false&limit=500&offset=${offset}`);
    if (!Array.isArray(arr) || !arr.length) break;
    all.push(...arr);
    if (arr.length < 500) break;
  }

  const shortRe = /(15\s*min|15\s*minute|15 minute|up\s*\/?\s*down|up or down)/i;
  const next = {};

  for (const [sym, cfg] of Object.entries(SYMBOLS)) {
    next[sym] = [];
    for (const m of all) {
      const ev = (m.events && m.events[0]) || {};
      const text = [m.question, m.slug, ev.title, ev.slug].filter(Boolean).join(' | ');
      if (!cfg.re.test(text) || !shortRe.test(text)) continue;

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
      if (!yesToken || !noToken) continue;

      next[sym].push({
        marketId: String(m.id),
        question: String(m.question || m.slug || m.id),
        eventTitle: String(ev.title || ev.slug || ''),
        endDate: m.endDate || ev.endDate || null,
        yesTokenId: yesToken,
        noTokenId: noToken,
        liquidity: safeNum(m.liquidityNum ?? m.liquidity, 0),
        volume24hr: safeNum(m.volume24hr, 0),
        yesAsk: null,
        noAsk: null,
      });
    }

    next[sym].sort((a, b) => (b.volume24hr - a.volume24hr) || (b.liquidity - a.liquidity));
    next[sym] = next[sym].slice(0, 8);
  }

  state.markets = next;
  state.lastRefreshAt = new Date().toISOString();
}

function bestAskFromBook(book) {
  if (!book || !Array.isArray(book.asks) || !book.asks.length) return null;
  const px = Number(book.asks[0].price);
  return Number.isFinite(px) ? px : null;
}

function attachPolyBooks() {
  const tokenMap = new Map();
  for (const [sym, arr] of Object.entries(state.markets)) {
    for (const m of arr) {
      tokenMap.set(m.yesTokenId, { sym, marketId: m.marketId, side: 'YES' });
      tokenMap.set(m.noTokenId, { sym, marketId: m.marketId, side: 'NO' });
    }
  }

  const tokenIds = [...tokenMap.keys()];
  if (!tokenIds.length) return;

  const ws = new WebSocket(POLY_WS);
  ws.addEventListener('open', () => {
    ws.send(JSON.stringify({ assets_ids: tokenIds, type: 'market', initial_dump: true, level: 2, custom_feature_enabled: true }));
  });
  ws.addEventListener('message', (ev) => {
    const raw = typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8');
    if (raw === 'PONG') return;
    let payload;
    try { payload = JSON.parse(raw); } catch { return; }

    const handle = (x) => {
      if (!x || typeof x !== 'object') return;
      if (x.event_type !== 'book') return;
      const tokenId = String(x.asset_id || x.token_id || '');
      const meta = tokenMap.get(tokenId);
      if (!meta) return;
      const arr = state.markets[meta.sym] || [];
      const m = arr.find((z) => z.marketId === meta.marketId);
      if (!m) return;
      const ask = bestAskFromBook({ asks: x.asks || [] });
      if (meta.side === 'YES') m.yesAsk = ask;
      else m.noAsk = ask;
      updateRecommendations(meta.sym);
    };

    if (Array.isArray(payload)) payload.forEach(handle);
    else handle(payload);
  });
  ws.addEventListener('close', () => setTimeout(attachPolyBooks, 3000));
  ws.addEventListener('error', () => {});
}

function ensureSource(symbol, source) {
  if (!state.sourcePrices[symbol]) state.sourcePrices[symbol] = {};
  if (!state.sourcePrices[symbol][source]) state.sourcePrices[symbol][source] = { price: null, ts: null };
  return state.sourcePrices[symbol][source];
}

function updateSource(symbol, source, price) {
  const rec = ensureSource(symbol, source);
  rec.price = price;
  rec.ts = Date.now();
  updateRecommendations(symbol);
}

function openWssSources() {
  for (const [sym, cfg] of Object.entries(SYMBOLS)) {
    if (cfg.binance) {
      const ws = new WebSocket(`wss://stream.binance.com:9443/ws/${cfg.binance}@bookTicker`);
      ws.addEventListener('message', (ev) => {
        try {
          const m = JSON.parse(typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8'));
          const px = safeNum(m.a, NaN) || safeNum(m.b, NaN);
          if (Number.isFinite(px)) updateSource(sym, 'binance', px);
        } catch {}
      });
    }
    if (cfg.coinbase) {
      const ws = new WebSocket('wss://advanced-trade-ws.coinbase.com');
      ws.addEventListener('open', () => ws.send(JSON.stringify({ type: 'subscribe', channel: 'ticker', product_ids: [cfg.coinbase] })));
      ws.addEventListener('message', (ev) => {
        try {
          const m = JSON.parse(typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8'));
          const px = safeNum(m?.events?.[0]?.tickers?.[0]?.price, NaN);
          if (Number.isFinite(px)) updateSource(sym, 'coinbase', px);
        } catch {}
      });
    }
    if (cfg.kraken) {
      const ws = new WebSocket('wss://ws.kraken.com/v2');
      ws.addEventListener('open', () => ws.send(JSON.stringify({ method: 'subscribe', params: { channel: 'ticker', symbol: [cfg.kraken] } })));
      ws.addEventListener('message', (ev) => {
        try {
          const m = JSON.parse(typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8'));
          const px = safeNum(m?.data?.[0]?.last, NaN);
          if (Number.isFinite(px)) updateSource(sym, 'kraken', px);
        } catch {}
      });
    }
    if (cfg.okx) {
      const ws = new WebSocket('wss://ws.okx.com:8443/ws/v5/public');
      ws.addEventListener('open', () => ws.send(JSON.stringify({ op: 'subscribe', args: [{ channel: 'tickers', instId: cfg.okx }] })));
      ws.addEventListener('message', (ev) => {
        try {
          const m = JSON.parse(typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8'));
          const t = m?.data?.[0] || {};
          const px = bestRealtimePx({ bid: t.bidPx, ask: t.askPx, last: t.last });
          if (Number.isFinite(px)) updateSource(sym, 'okx', px);
        } catch {}
      });
    }
    if (cfg.bybit) {
      const ws = new WebSocket('wss://stream.bybit.com/v5/public/spot');
      ws.addEventListener('open', () => ws.send(JSON.stringify({ op: 'subscribe', args: [`tickers.${cfg.bybit}`] })));
      ws.addEventListener('message', (ev) => {
        try {
          const m = JSON.parse(typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8'));
          const px = safeNum(m?.data?.lastPrice, NaN);
          if (Number.isFinite(px)) updateSource(sym, 'bybit', px);
        } catch {}
      });
    }
  }
}

function aggregateSignal(symbol) {
  const src = state.sourcePrices[symbol] || {};
  const fresh = Object.entries(src).filter(([, v]) => v && v.price && v.ts && (Date.now() - v.ts) < 2000);
  if (!fresh.length) return null;
  const prices = fresh.map(([, v]) => v.price);
  const mean = prices.reduce((a, b) => a + b, 0) / prices.length;
  return {
    sourceCount: fresh.length,
    meanPrice: +mean.toFixed(6),
    prices: Object.fromEntries(fresh.map(([k, v]) => [k, v.price])),
  };
}

function scoreMarket(m, signal) {
  if (!signal || m.yesAsk == null || m.noAsk == null) return null;
  const endTs = m.endDate ? Date.parse(m.endDate) : NaN;
  const msToEnd = Number.isFinite(endTs) ? (endTs - Date.now()) : null;
  const closer = msToEnd != null ? Math.max(0, Math.min(1, 1 - msToEnd / (15 * 60 * 1000))) : 0;
  const minAsk = Math.min(m.yesAsk, m.noAsk);
  const imbalance = +(0.5 - minAsk).toFixed(4);
  const score = +(imbalance * 100 + closer * 20 + Math.log1p(m.volume24hr || 0) + Math.log1p(m.liquidity || 0)).toFixed(3);
  const side = m.yesAsk < m.noAsk ? 'YES' : 'NO';
  return { side, score, imbalance, msToEnd, sourceCount: signal.sourceCount };
}

function updateRecommendations(symbol) {
  const signal = aggregateSignal(symbol);
  const arr = state.markets[symbol] || [];
  const scored = arr
    .map((m) => ({
      marketId: m.marketId,
      question: m.question,
      endDate: m.endDate,
      yesAsk: m.yesAsk,
      noAsk: m.noAsk,
      volume24hr: m.volume24hr,
      liquidity: m.liquidity,
      signal,
      rec: scoreMarket(m, signal),
    }))
    .filter((x) => x.rec)
    .sort((a, b) => b.rec.score - a.rec.score);
  state.recommendations[symbol] = scored.slice(0, 5);
}

function listDataFiles(pattern) {
  try {
    return fs.readdirSync(DATA_DIR)
      .filter((name) => pattern.test(name))
      .sort((a, b) => {
        const aLive = a.includes('_live_') ? 1 : 0;
        const bLive = b.includes('_live_') ? 1 : 0;
        if (aLive !== bLive) return bLive - aLive;
        return 0;
      })
      .map((name) => {
        const filePath = path.join(DATA_DIR, name);
        const st = fs.statSync(filePath);
        return { name, path: filePath, mtimeMs: st.mtimeMs };
      })
      .sort((a, b) => b.mtimeMs - a.mtimeMs);
  } catch {
    return [];
  }
}

function readJsonSafe(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return null;
  }
}

function readJsonlSafe(filePath) {
  try {
    return fs.readFileSync(filePath, 'utf8')
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        try { return JSON.parse(line); } catch { return null; }
      })
      .filter(Boolean);
  } catch {
    return [];
  }
}

function stripMetaSuffix(name) {
  return name.replace(/_(summary|status)\.json$/, '');
}

function stripLedgerSuffix(name) {
  return name.replace(/_ledger\.jsonl$/, '');
}

function summarizeBlockers(decisions) {
  const counts = new Map();
  for (const d of decisions || []) {
    for (const b of d?.blockers || []) counts.set(b, (counts.get(b) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([blocker, count]) => ({ blocker, count }));
}

function loadPaperState() {
  const metaFiles = listDataFiles(/^polymarket_crypto_paperbot.*_(summary|status)\.json$/);
  const statusFiles = listDataFiles(/^polymarket_crypto_paperbot.*_status\.json$/);
  const ledgerFiles = listDataFiles(/^polymarket_crypto_paperbot.*_ledger\.jsonl$/);
  const signalFiles = listDataFiles(/^polymarket_crypto_paperbot.*_signals\.jsonl$/);

  const liveSummaryPath = path.join(DATA_DIR, 'polymarket_crypto_paperbot_multi_live_summary.json');
  const liveStatusPath = path.join(DATA_DIR, 'polymarket_crypto_paperbot_multi_live_status.json');
  const liveLedgerPath = path.join(DATA_DIR, 'polymarket_crypto_paperbot_multi_live_ledger.jsonl');
  const liveSignalsPath = path.join(DATA_DIR, 'polymarket_crypto_paperbot_multi_live_signals.jsonl');

  const latestMeta = fs.existsSync(liveSummaryPath)
    ? { name: path.basename(liveSummaryPath), path: liveSummaryPath, mtimeMs: fs.statSync(liveSummaryPath).mtimeMs }
    : (metaFiles.find((x) => x.name.endsWith('_summary.json')) || metaFiles[0] || null);
  const latestStatusFile = fs.existsSync(liveStatusPath)
    ? { name: path.basename(liveStatusPath), path: liveStatusPath, mtimeMs: fs.statSync(liveStatusPath).mtimeMs }
    : (statusFiles[0] || null);
  const latestSummary = latestMeta ? readJsonSafe(latestMeta.path) : null;
  const latestStatus = latestStatusFile ? readJsonSafe(latestStatusFile.path) : null;
  const matchedLedger = fs.existsSync(liveLedgerPath)
    ? { name: path.basename(liveLedgerPath), path: liveLedgerPath, mtimeMs: fs.statSync(liveLedgerPath).mtimeMs }
    : (latestMeta ? ledgerFiles.find((x) => stripLedgerSuffix(x.name) === stripMetaSuffix(latestMeta.name)) : null);
  const matchedSignals = fs.existsSync(liveSignalsPath)
    ? { name: path.basename(liveSignalsPath), path: liveSignalsPath, mtimeMs: fs.statSync(liveSignalsPath).mtimeMs }
    : (latestMeta ? signalFiles.find((x) => x.name.replace(/_signals\.jsonl$/, '') === stripMetaSuffix(latestMeta.name)) : signalFiles[0] || null);
  const ledgerRows = matchedLedger ? readJsonlSafe(matchedLedger.path) : [];
  const signalRows = matchedSignals ? readJsonlSafe(matchedSignals.path) : [];
  const trades = ledgerRows.filter((x) => x.type === 'open' || x.type === 'close').slice(-18).reverse();
  const activity = [
    ...ledgerRows.filter((x) => x.type !== 'open' && x.type !== 'close').map((x) => ({ channel: 'ledger', ...x })),
    ...signalRows.filter((x) => x.type === 'open_decision' || x.type === 'settle_decision' || x.type === 'status_report').map((x) => ({ channel: 'signals', ...x })),
  ].sort((a, b) => Date.parse(b.ts || 0) - Date.parse(a.ts || 0)).slice(0, 20);
  const heartbeatRef = latestStatus?.ts || (latestStatusFile ? new Date(latestStatusFile.mtimeMs).toISOString() : null);
  const lagSec = agoSeconds(heartbeatRef);
  const botOnline = lagSec != null && lagSec <= 120;
  const botMode = latestStatus?.liveMarketsFound > 0 ? 'trading-capable' : 'log-only';

  state.paper = {
    budget: {
      totalBudget: PAPER_PLAN.totalBudget,
      todayCostApplied: PAPER_PLAN.todayCostApplied,
      dailyRunRate: PAPER_PLAN.dailyRunRate,
      netBudget: +(PAPER_PLAN.totalBudget - PAPER_PLAN.todayCostApplied).toFixed(2),
    },
    latestSummaryFile: latestMeta ? latestMeta.name : null,
    latestSummaryTs: latestSummary?.ts || null,
    latestStatusFile: latestStatusFile ? latestStatusFile.name : null,
    latestStatusMtime: latestStatusFile ? new Date(latestStatusFile.mtimeMs).toISOString() : null,
    latestStatus,
    botOnline,
    botMode,
    botLagSec: lagSec,
    stats: latestSummary?.stats || latestStatus?.stats || null,
    chosenMarkets: latestSummary?.chosenMarkets || latestStatus?.chosenMarkets || [],
    tradeLogFile: matchedLedger ? matchedLedger.name : null,
    tradeLogMatched: !!matchedLedger,
    tradeLogNote: matchedLedger
      ? '当前显示的是 latest run 的真实 ledger'
      : '当前没有匹配到同 run 的真实 ledger；为杜绝幻觉，面板不会回退显示旧交易记录',
    signalsFile: matchedSignals ? matchedSignals.name : null,
    trades,
    activity,
    blockerCounts: summarizeBlockers(latestStatus?.decisions || []),
    perSymbolLearning: latestStatus?.perSymbolLearning || {},
    decisions: latestStatus?.decisions || [],
  };
}

function render() {
  const budget = state.paper?.budget || {};
  const stats = state.paper?.stats || {};
  const notesText = state.notes.slice(0, 8).map((x) => `[${fmtDate(x.ts)}] ${x.msg}`).join('\n') || '暂无运行备注';
  const preferredSourceOrder = ['binance', 'okx', 'coinbase', 'kraken', 'bybit'];
  const chosenCount = Array.isArray(state.paper?.chosenMarkets) ? state.paper.chosenMarkets.length : 0;
  const lastTradeTs = state.paper?.trades?.[0]?.ts || '—';
  const status = state.paper.latestStatus || {};
  const botOnline = !!state.paper.botOnline;
  const botLagSec = state.paper.botLagSec;
  const blockerCounts = state.paper.blockerCounts || [];
  const perSymbolLearning = state.paper.perSymbolLearning || {};
  const decisions = state.paper.decisions || [];

  function sourceLine(signal) {
    if (!signal || !signal.prices) return '暂无实时价格';
    const xs = preferredSourceOrder
      .filter((name) => signal.prices[name] != null)
      .map((name) => `${name}: ${signal.prices[name]}`);
    return xs.length ? xs.join(' · ') : '暂无实时价格';
  }

  function topRecSummary(rec) {
    if (!rec) return '暂无可执行 setup';
    const bestAsk = rec.rec.side === 'YES' ? rec.yesAsk : rec.noAsk;
    return `${rec.rec.side} · 价位 ${bestAsk ?? '—'} · 评分 ${rec.rec.score} · ${minsText(rec.rec.msToEnd)}`;
  }

  const topStats = [
    { label: '机器人', value: botOnline ? '在线' : '离线', note: botLagSec == null ? '暂无心跳' : `延迟 ${fmtSec(botLagSec)}`, cls: botOnline ? 'good' : 'bad' },
    { label: '模式', value: state.paper.botMode || 'unknown', note: `${safeNum(status.liveMarketsFound, 0)} 个 live 市场`, cls: safeNum(status.liveMarketsFound, 0) ? 'good' : 'warn' },
    { label: '纸面净值', value: fmtDollar(10), note: `净预算 ${fmtDollar(budget.netBudget)}`, cls: '' },
    { label: '已实现盈亏', value: stats.realizedPnl != null ? fmtSignedDollar(stats.realizedPnl) : '—', note: `已平仓 ${safeNum(stats.closedTrades, 0)} 笔`, cls: toneClass(stats.realizedPnl) },
    { label: '最近成交', value: lastTradeTs === '—' ? '—' : fmtTs(lastTradeTs), note: lastTradeTs === '—' ? '暂无成交' : fmtDate(lastTradeTs), cls: '' },
  ].map((x) => `
    <div class="kpi-card">
      <div class="kpi-label">${esc(x.label)}</div>
      <div class="kpi-value ${esc(x.cls)}">${esc(x.value)}</div>
      <div class="kpi-note">${esc(x.note)}</div>
    </div>
  `).join('');

  const tradeRows = state.paper.trades.length
    ? state.paper.trades.map((t) => {
      const symbol = t.symbol || symbolFromText(t.question) || '—';
      const isClose = t.type === 'close';
      const pnlNum = Number(t.pnl);
      const statusPill = !isClose ? '开仓记录' : (pnlNum > 0 ? '盈利' : (pnlNum < 0 ? '亏损' : '持平'));
      const statusClass = !isClose ? 'status-live' : (pnlNum > 0 ? 'status-win' : (pnlNum < 0 ? 'status-loss' : 'status-flat'));
      const priceText = !isClose
        ? `${Number.isFinite(Number(t.entryPrice)) ? Number(t.entryPrice).toFixed(4) : '—'} / ${Number.isFinite(Number(t.shares)) ? Number(t.shares).toFixed(4) : '—'}sh`
        : `${fmtDollar(t.payout)} payout`;
      const pnlText = isClose ? fmtSignedDollar(t.pnl) : '开仓事件';
      return `
        <tr class="${isClose ? 'row-close' : 'row-open'}">
          <td class="mono trade-time-cell"><div class="trade-time-main">${esc(fmtTs(t.ts || '—'))}</div><div class="trade-time-sub">${esc(fmtDate(t.ts || '—'))}</div></td>
          <td><span class="status-pill ${statusClass}">${esc(statusPill)}</span></td>
          <td class="symbol-cell">${esc(symbol)}</td>
          <td class="question">${esc(t.question || t.marketId || '—')}</td>
          <td class="mono">${esc(t.side || '—')}</td>
          <td class="mono">${esc(t.stake != null ? fmtDollar(t.stake) : '—')}</td>
          <td class="mono">${esc(priceText)}</td>
          <td class="mono">${esc(t.balance != null ? fmtDollar(t.balance) : '—')}</td>
          <td class="mono ${statusClass}">${esc(pnlText)}</td>
        </tr>
      `;
    }).join('')
    : '<tr><td colspan="9" class="empty-state">暂无最近成交。当前 ledger 里还没有新的 paper trade。</td></tr>';

  const noTradeRows = decisions.length
    ? decisions.slice(0, 12).map((d) => {
      const blockersText = orderedBlockers(d.blockers || []).slice(0, 3).map(blockerLabel).join('，') || (d.pass ? '通过' : '无 blocker');
      return `
        <tr>
          <td class="symbol-cell">${esc(d.symbol || '—')}</td>
          <td class="mono">${esc(d.direction || '—')}</td>
          <td class="mono ${pillClass((d.estimatedWinProb || 0) - 0.5)}">${esc(fmtPct((d.estimatedWinProb || 0) * 100))}</td>
          <td class="mono">${esc(fmtBps(d.roundQuality, 2))}</td>
          <td class="mono">${esc(fmtSec(d.secondsSinceLastFlip))}</td>
          <td class="mono">${esc(blockersText)}</td>
        </tr>
      `;
    }).join('')
    : '<tr><td colspan="6" class="empty-state">最新状态里还没有 decision board。</td></tr>';

  const roundRows = Object.keys(SYMBOLS).map((sym) => {
    const ps = perSymbolLearning[sym] || {};
    const latest = ps.roundLatestCompleted || ps.roundCurrent || null;
    const leader = ps.leader?.composite || null;
    const topDecision = ps.topDecision || null;
    const latestFlip = latest?.flips?.length ? latest.flips[latest.flips.length - 1] : null;
    const flipTimeline = latest?.flipSecondsFromOpen?.length ? latest.flipSecondsFromOpen.slice(0, 5).map((x) => `${x}s`).join(' · ') : '—';
    const momentum = [ps.leader?.mom5s?.moveBps, ps.leader?.mom15s?.moveBps, ps.leader?.mom60s?.moveBps]
      .map((x) => Number.isFinite(Number(x)) ? Number(x).toFixed(1) : '—').join(' / ');
    return `
      <tr>
        <td class="symbol-cell">${esc(sym)}</td>
        <td class="mono">${esc(latest?.openPx != null ? String(latest.openPx) : '—')}</td>
        <td class="mono ${pillClass(latest?.netMoveBps)}">${esc(fmtBps(latest?.netMoveBps))}</td>
        <td class="mono">${esc(String(latest?.flipCount ?? '—'))}</td>
        <td class="mono">${esc(latestFlip ? `${fmtTs(latestFlip.ts)} / ${fmtSec(latestFlip.secondsFromRoundOpen)}` : '—')}</td>
        <td class="mono">${esc(flipTimeline)}</td>
        <td class="mono">${esc(momentum)}</td>
        <td class="mono">${esc(leader ? `${leader.sourceCount} 源 / ${fmtBps(leader.spreadBps, 2)}` : '—')}</td>
        <td class="mono">${esc(topDecision ? (topDecision.pass ? '通过' : orderedBlockers(topDecision.blockers || []).slice(0, 2).map(blockerLabel).join('，')) : '—')}</td>
      </tr>
    `;
  }).join('');

  const blockerPills = blockerCounts.length
    ? blockerCounts.map((x) => `<div class="blocker-pill"><span>${esc(x.blocker)}</span><b>${esc(String(x.count))}</b></div>`).join('')
    : '<div class="empty-mini">暂时还没有 blocker 统计</div>';

  const nearestSetup = (decisions || []).slice().sort((a, b) => {
    const aBlockers = (a.blockers || []).length;
    const bBlockers = (b.blockers || []).length;
    const aPenalty = Math.max(0, safeNum(a.flipCount, 0) - 2) * 0.03 + Math.max(0, 4 - safeNum(a.roundQuality, 0)) * 0.02;
    const bPenalty = Math.max(0, safeNum(b.flipCount, 0) - 2) * 0.03 + Math.max(0, 4 - safeNum(b.roundQuality, 0)) * 0.02;
    const aScore = safeNum(a.estimatedWinProb, 0) - aBlockers * 0.08 - aPenalty;
    const bScore = safeNum(b.estimatedWinProb, 0) - bBlockers * 0.08 - bPenalty;
    return bScore - aScore || aBlockers - bBlockers || safeNum(b.roundQuality, 0) - safeNum(a.roundQuality, 0);
  })[0] || null;
  const nearestSetupBlockers = nearestSetup ? orderedBlockers(nearestSetup.blockers || []) : [];
  const nearestSetupProgress = nearestSetup ? Math.max(0, Math.min(100, Math.round((1 - nearestSetupBlockers.length / 6) * 100))) : 0;
  const firstTradeWatch = nearestSetup ? `
    <div class="side-section first-watch">
      <div class="side-title">最接近成交</div>
      <div class="watch-headline">${esc(nearestSetup.symbol || '—')} · ${esc(nearestSetup.direction || '—')} · ${esc(fmtPct((nearestSetup.estimatedWinProb || 0) * 100))}</div>
      <div class="watch-detail">目标价 ${esc(nearestSetup.desiredAsk != null ? String(nearestSetup.desiredAsk) : '—')} · 滞后 ${esc(nearestSetup.polyLag != null ? nearestSetup.polyLag.toFixed(4) : '—')} · flips ${esc(String(nearestSetup.flipCount ?? '—'))}</div>
      <div class="watch-detail">质量 ${esc(nearestSetup.roundQuality != null ? nearestSetup.roundQuality.toFixed(2) : '—')} · spread ${esc(fmtBps(nearestSetup.sourceSpreadBps, 2))}</div>
      <div class="progress-meta"><span>通过进度</span><b>${esc(String(nearestSetupProgress))}%</b></div>
      <div class="progress-bar"><div class="progress-fill" style="width:${nearestSetupProgress}%"></div></div>
      <div class="watch-detail">还差 ${esc(String(nearestSetupBlockers.length))} 项条件</div>
      <div class="watch-blockers">${esc(nearestSetupBlockers.length ? `仍缺条件：${nearestSetupBlockers.slice(0, 5).map(blockerLabel).join(' · ')}` : '当前已通过')}</div>
    </div>
  ` : `
    <div class="side-section first-watch">
      <div class="side-title">最接近成交</div>
      <div class="empty-mini">暂时还没有接近可成交的 setup。</div>
    </div>
  `;

  const activityRows = (state.paper.activity || []).length
    ? state.paper.activity.map((x) => {
      const type = x.type || 'event';
      const symbol = x.symbol || '—';
      let detail = '';
      if (type === 'early_exit_signal') detail = `${x.reason || '退出'} · ${x.currentEval?.direction || '—'} · ${x.currentEval?.question || x.marketId || ''}`;
      else if (type === 'open_decision') detail = `${x.direction || '—'} · ${x.question || x.marketId || ''}`;
      else if (type === 'settle_decision') detail = `${x.won ? '盈利' : '亏损'} · ${x.roundSummary?.roundEndTs || x.marketId || ''}`;
      else if (type === 'status_report') detail = `余额 ${fmtDollar(x.stats?.balance)} · 持仓 ${safeNum(x.stats?.openTrades, 0)} · 已平仓 ${safeNum(x.stats?.closedTrades, 0)}`;
      else detail = x.reason || x.marketId || '';
      return `
        <tr>
          <td class="mono trade-time-cell"><div class="trade-time-main">${esc(fmtTs(x.ts || '—'))}</div><div class="trade-time-sub">${esc(fmtDate(x.ts || '—'))}</div></td>
          <td class="mono">${esc(type)}</td>
          <td class="symbol-cell">${esc(symbol)}</td>
          <td class="question">${esc(detail)}</td>
        </tr>
      `;
    }).join('')
    : '<tr><td colspan="4" class="empty-state">暂时还没有 activity feed。</td></tr>';

  const matchedMarketRows = (state.paper.chosenMarkets || []).length
    ? state.paper.chosenMarkets.map((m) => `
      <tr>
        <td class="symbol-cell">${esc(m.symbol || '—')}</td>
        <td class="question">${esc(m.question || m.marketId || '—')}</td>
        <td class="mono">${esc(fmtTs(m.endDate || '—'))}</td>
        <td class="mono">${esc(fmtDollar(m.liquidity))}</td>
        <td class="mono">${esc(fmtDollar(m.volume24hr))}</td>
      </tr>
    `).join('')
    : '<tr><td colspan="5" class="empty-state">暂时还没有命中的 live 市场。</td></tr>';

  const watchlistCards = Object.keys(SYMBOLS).map((sym) => {
    const signal = aggregateSignal(sym);
    const recs = state.recommendations[sym] || [];
    const top = recs[0] || null;
    const ps = perSymbolLearning[sym] || {};
    const latest = ps.roundLatestCompleted || ps.roundCurrent || null;
    const flipInfo = latest ? `${latest.flipCount} 次翻转 · 最近 ${latest.flips?.length ? fmtSec(latest.flips[latest.flips.length - 1].secondsFromRoundOpen) : '—'}` : '暂无本轮数据';
    const priceLine = sourceLine(signal);
    return `
      <div class="watch-card">
        <div class="watch-top">
          <div class="watch-symbol">${esc(sym)}</div>
          <div class="watch-badge">${signal ? `${signal.sourceCount} 源` : '0 源'}</div>
        </div>
        <div class="watch-price">${esc(priceLine)}</div>
        <div class="watch-signal ${top ? '' : 'muted'}">${esc(topRecSummary(top))}</div>
        <div class="watch-mini">${esc(flipInfo)}</div>
      </div>
    `;
  }).join('');

  const metaPills = `
    <div class="meta-row">
      <div class="meta-pill">状态文件：${esc(state.paper.latestStatusFile || '—')}</div>
      <div class="meta-pill">摘要文件：${esc(state.paper.latestSummaryFile || '—')}</div>
      <div class="meta-pill">交易日志：${esc(state.paper.tradeLogFile || '—')}</div>
      <div class="meta-pill">命中市场：${esc(chosenCount)}</div>
      <div class="meta-pill">状态时间：${esc(fmtDate(status.ts || state.paper.latestStatusMtime || '—'))}</div>
    </div>
  `;

  const opsPanel = `
    <div class="side-section">
      <div class="side-title">机器人状态</div>
      <div class="ops-grid">
        <div class="ops-item"><span>机器人</span><b class="${botOnline ? 'good' : 'bad'}">${esc(botOnline ? '在线' : '离线')}</b></div>
        <div class="ops-item"><span>模式</span><b>${esc(state.paper.botMode || 'unknown')}</b></div>
        <div class="ops-item"><span>最近心跳</span><b>${esc(fmtTs(status.ts || state.paper.latestStatusMtime || '—'))}</b></div>
        <div class="ops-item"><span>延迟</span><b>${esc(fmtSec(botLagSec))}</b></div>
        <div class="ops-item"><span>持仓中</span><b>${esc(safeNum(stats.openTrades, 0))}</b></div>
        <div class="ops-item"><span>已平仓</span><b>${esc(safeNum(stats.closedTrades, 0))}</b></div>
      </div>
      <div class="ops-note">${esc(state.paper.tradeLogNote || '')}</div>
    </div>
  `;

  const blockerPanel = `
    <div class="side-section">
      <div class="side-title">不交易 / 阻塞原因</div>
      <div class="blocker-grid">${blockerPills}</div>
    </div>
  `;

  const strategyPanel = `
    <div class="side-section">
      <div class="side-title">策略快照</div>
      <ul class="strategy-list">
        <li>当前重点：短周期 crypto 合约 / paper execution</li>
        <li>领先源：Binance / OKX / Coinbase</li>
        <li>核心优势：lag + momentum + 多源一致性</li>
        <li>当前模式：${esc(state.paper.botMode || 'unknown')}</li>
        <li>已发现 live 市场：${esc(String(safeNum(status.liveMarketsFound, 0)))}</li>
        <li>signals 文件：${esc(state.paper.signalsFile || '—')}</li>
      </ul>
    </div>
  `;

  const notesPanel = `
    <div class="side-section notes-panel">
      <div class="side-title">最近运行备注</div>
      <pre>${esc(notesText)}</pre>
    </div>
  `;

  const body = `<!doctype html>
<html><head><meta charset="utf-8"><title>Polymarket Bot Cockpit</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Ctext y='50%25' x='50%25' dominant-baseline='central' text-anchor='middle' font-size='52'%3E9%EF%B8%8F%E2%83%A3%3C/text%3E%3C/svg%3E">
<style>
:root{
  --bg:#f3eee3;
  --panel:#fbf7ef;
  --panel-soft:#f6f0e4;
  --line:#ddd2bc;
  --line-soft:#e7dcc7;
  --ink:#201a14;
  --muted:#7f7467;
  --accent:#8a6f43;
  --accent-2:#b3925d;
  --good:#1f8b5d;
  --warn:#b07a12;
  --bad:#bb4d3e;
  --shadow:0 8px 24px rgba(77,57,28,.08);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink)}
body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh;overflow:auto}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
.shell{width:min(2480px, calc(100vw * 5 / 7));margin:12px auto;display:grid;grid-template-rows:auto auto auto minmax(0,1fr);gap:12px}
.hero,.kpi-strip,.panel,.side-section{background:linear-gradient(180deg, rgba(251,247,239,.98) 0%, rgba(246,240,228,.98) 100%);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow)}
.hero{padding:14px 18px;display:flex;justify-content:space-between;align-items:end;gap:18px}
.eyebrow{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--accent)}
.hero h1{margin:6px 0 4px;font-size:28px;line-height:1.04;font-weight:700}
.hero p{margin:0;font-size:13px;color:var(--muted);max-width:860px}
.meta-row{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end}
.meta-pill,.watch-badge,.status-pill,.blocker-pill{display:inline-flex;align-items:center;justify-content:center;border-radius:999px;border:1px solid var(--line);padding:6px 10px;font-size:11px;font-weight:700;letter-spacing:.04em;white-space:nowrap}
.meta-pill{background:#f8f1e4;color:#6d5835}
.kpi-strip{padding:12px;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}
.kpi-card{background:rgba(255,255,255,.55);border:1px solid var(--line-soft);border-radius:14px;padding:12px 14px;min-height:90px}
.kpi-label{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);font-weight:700}
.kpi-value{margin-top:6px;font-size:28px;line-height:1.02;font-weight:700}
.kpi-note{margin-top:6px;font-size:12px;color:var(--muted)}
.good{color:var(--good)} .bad{color:var(--bad)} .warn{color:var(--warn)}
.pill-good{color:var(--good)} .pill-bad{color:var(--bad)} .pill-warn{color:var(--warn)} .pill-muted{color:var(--muted)}
.main-grid{display:grid;grid-template-columns:minmax(0,1.9fr) 430px;gap:12px;min-height:0}
.left-grid{display:grid;grid-template-rows:minmax(0,1.02fr) minmax(0,.95fr) minmax(0,.95fr) minmax(0,1fr);gap:12px;min-height:0}
.panel{padding:14px 16px;display:grid;grid-template-rows:auto minmax(0,1fr);min-height:0}
.split-panel{padding-bottom:12px}
.split-grid{display:grid;grid-template-columns:1.08fr .92fr;gap:12px;min-height:0}
.split-col{display:grid;grid-template-rows:auto minmax(0,1fr);min-height:0}
.panel-head{display:flex;justify-content:space-between;align-items:end;gap:12px;margin-bottom:10px}
.compact-head{margin-bottom:8px}
.panel-title{margin:0;font-size:22px;font-weight:700;letter-spacing:.01em}
.panel-sub{margin:4px 0 0;font-size:12px;color:var(--muted)}
.table-shell{border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.45);overflow:hidden;min-height:0}
.split-shell{height:100%;min-height:280px}
.scroll{height:100%;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:10px 11px;border-bottom:1px solid rgba(221,210,188,.9);text-align:left;vertical-align:top}
th{position:sticky;top:0;z-index:2;background:#f6f0e4;color:#74695a;font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}
tbody tr:hover{background:rgba(138,111,67,.05)}
.row-open{background:rgba(31,139,93,.03)}
.row-close{background:rgba(255,255,255,.18)}
.question{min-width:520px;max-width:900px;line-height:1.35}
.symbol-cell{font-weight:700;letter-spacing:.04em}
.tight{white-space:nowrap}
.trade-time-cell{min-width:158px}
.trade-time-main{font-size:15px;font-weight:800;line-height:1.1;color:var(--ink)}
.trade-time-sub{margin-top:4px;font-size:11px;line-height:1.2;color:var(--muted)}
.empty-state,.empty-mini{padding:28px 16px;color:var(--muted);text-align:center;font-size:13px}
.status-pill{min-width:58px;padding:5px 10px;background:#efe6d5;color:#6a5944}
.status-live{color:#1f8b5d;border-color:rgba(31,139,93,.24);background:rgba(31,139,93,.08)}
.status-win{color:#1f8b5d;border-color:rgba(31,139,93,.24);background:rgba(31,139,93,.08)}
.status-loss{color:#bb4d3e;border-color:rgba(187,77,62,.24);background:rgba(187,77,62,.08)}
.status-flat{color:#b07a12;border-color:rgba(176,122,18,.24);background:rgba(176,122,18,.08)}
.sidebar{display:grid;grid-template-rows:auto auto auto minmax(0,1fr);gap:12px;min-height:0}
.side-section{padding:14px}
.side-title{font-size:12px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);margin-bottom:10px}
.watch-grid{display:grid;grid-template-columns:1fr;gap:8px}
.watch-card{background:rgba(255,255,255,.42);border:1px solid var(--line-soft);border-radius:14px;padding:10px 11px}
.watch-top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.watch-symbol{font-size:17px;font-weight:800;line-height:1}
.watch-badge{background:#f8f1e4;color:#6d5835}
.watch-price{margin-top:7px;font-size:11px;line-height:1.4;color:var(--muted);min-height:34px}
.watch-signal{margin-top:7px;font-size:12px;font-weight:650;line-height:1.35}
.watch-mini{margin-top:6px;font-size:11px;color:var(--muted)}
.ops-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.ops-item{background:rgba(255,255,255,.42);border:1px solid var(--line-soft);border-radius:12px;padding:10px}
.ops-item span{display:block;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:700}
.ops-item b{display:block;margin-top:6px;font-size:18px;line-height:1.1}
.ops-note{margin-top:10px;font-size:12px;line-height:1.45;color:var(--muted)}
.strategy-list{margin:0;padding-left:18px;color:var(--muted);font-size:12px;line-height:1.55}
.first-watch{border:1px solid rgba(138,111,67,.22);background:linear-gradient(180deg, rgba(255,251,241,.98) 0%, rgba(247,239,222,.98) 100%)}
.watch-headline{font-size:18px;font-weight:800;line-height:1.15;color:var(--ink)}
.watch-detail{margin-top:8px;font-size:12px;line-height:1.45;color:var(--muted)}
.progress-meta{margin-top:10px;display:flex;justify-content:space-between;align-items:center;font-size:11px;letter-spacing:.04em;color:var(--muted);font-weight:700}
.progress-bar{margin-top:6px;height:8px;border-radius:999px;background:rgba(138,111,67,.12);overflow:hidden}
.progress-fill{height:100%;border-radius:999px;background:linear-gradient(90deg, #b07a12 0%, #8a6f43 55%, #1f8b5d 100%)}
.watch-blockers{margin-top:10px;font-size:12px;line-height:1.45;color:var(--bad);font-weight:700}
.notes-panel{min-height:0;display:grid;grid-template-rows:auto minmax(0,1fr)}
.notes-panel pre{margin:0;overflow:auto;white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.5;color:var(--muted)}
.blocker-grid{display:flex;flex-wrap:wrap;gap:8px}
.blocker-pill{background:#f8f1e4;color:#6d5835;gap:8px}
.blocker-pill b{font-size:12px}
@media (max-width: 2100px){
  .main-grid{grid-template-columns:minmax(0,1.6fr) 390px}
}
@media (max-width: 1680px){
  .main-grid{grid-template-columns:1fr}
  .sidebar{grid-template-columns:1fr 1fr;grid-template-rows:auto auto}
}
@media (max-width: 980px){
  .kpi-strip{grid-template-columns:1fr 1fr}
  .hero{flex-direction:column;align-items:flex-start}
  .meta-row{justify-content:flex-start}
  .sidebar{grid-template-columns:1fr}
}
</style></head><body>
<div class="shell">
  <section class="hero">
    <div>
      <div class="eyebrow">自动交易控制台 · 纸面交易台</div>
      <h1>自动交易控制台 / Bot cockpit</h1>
      <p>前台先做成真实可信的 bot control panel：主屏优先展示交易详情、决策拒绝原因、7 币 round intelligence 和 flip 时间；页面时间统一按 GMT+8 / Asia/Shanghai 显示；后台策略后面只保留一个单实例无间断 runner。</p>
    </div>
    ${metaPills}
  </section>

  <section class="kpi-strip">${topStats}</section>

  <section class="main-grid">
    <section class="left-grid">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">交易明细</h2>
            <p class="panel-sub">历史开仓/平仓事件与余额变化；是否仍在持仓，以「机器人状态 → 持仓中」为准。</p>
          </div>
        </div>
        <div class="table-shell scroll">
          <table>
            <thead>
              <tr><th>交易时间<br>GMT+8</th><th>状态</th><th>币种</th><th class="question">市场 / 合约</th><th>方向</th><th>投入</th><th>价格 / 数量</th><th>余额</th><th>盈亏</th></tr>
            </thead>
            <tbody>${tradeRows}</tbody>
          </table>
        </div>
      </section>

      <section class="panel split-panel">
        <div class="split-grid">
          <div class="split-col">
            <div class="panel-head compact-head">
              <div>
                <h2 class="panel-title">运行事件流</h2>
                <p class="panel-sub">执行尝试 / 提前退出 / 状态心跳</p>
              </div>
            </div>
            <div class="table-shell split-shell">
              <div class="scroll">
                <table>
                  <thead>
                    <tr><th>时间<br>GMT+8</th><th>事件</th><th>币种</th><th class="question">详情</th></tr>
                  </thead>
                  <tbody>${activityRows}</tbody>
                </table>
              </div>
            </div>
          </div>
          <div class="split-col">
            <div class="panel-head compact-head">
              <div>
                <h2 class="panel-title">命中市场</h2>
                <p class="panel-sub">scanner 当前实际盯住的 live 合约</p>
              </div>
            </div>
            <div class="table-shell split-shell">
              <div class="scroll">
                <table>
                  <thead>
                    <tr><th>币种</th><th class="question">合约</th><th>结束时间<br>GMT+8</th><th>流动性</th><th>24h成交量</th></tr>
                  </thead>
                  <tbody>${matchedMarketRows}</tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">不交易决策</h2>
            <p class="panel-sub">不成交不是空白，而是策略过滤器本身的信号。这里直接看通过 / blocker。</p>
          </div>
        </div>
        <div class="table-shell scroll">
          <table>
            <thead>
              <tr><th>币种</th><th>方向</th><th>胜率估计</th><th>Round 质量</th><th>距上次翻转</th><th>主要 blocker</th></tr>
            </thead>
            <tbody>${noTradeRows}</tbody>
          </table>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">7币种 round 看板</h2>
            <p class="panel-sub">每个 15m round 的开盘价、翻转次数、flip 时间、momentum、source 质量与当前决策。</p>
          </div>
        </div>
        <div class="table-shell scroll">
          <table>
            <thead>
              <tr><th>币种</th><th>开盘价</th><th>波动</th><th>翻转数</th><th>最近翻转</th><th>翻转时间线</th><th>动量 5/15/60</th><th>来源</th><th>决策</th></tr>
            </thead>
            <tbody>${roundRows}</tbody>
          </table>
        </div>
      </section>
    </section>

    <aside class="sidebar">
      <section class="side-section">
        <div class="side-title">观察列表</div>
        <div class="watch-grid">${watchlistCards}</div>
      </section>
      ${firstTradeWatch}
      ${opsPanel}
      ${blockerPanel}
      ${strategyPanel}
      ${notesPanel}
    </aside>
  </section>
</div>
</body></html>`;

  return body;
}

async function bootstrap() {
  await loadMarkets();
  loadPaperState();
  logNote('Markets loaded');
  logNote('Paper state loaded');
  openWssSources();
  attachPolyBooks();
  setInterval(async () => {
    try {
      await loadMarkets();
      loadPaperState();
      logNote('Markets refreshed');
    } catch (err) {
      logNote('Refresh error: ' + (err.message || String(err)));
    }
  }, 60_000);
}

const server = http.createServer((req, res) => {
  loadPaperState();
  if (req.url === '/api/state') {
    res.setHeader('content-type', 'application/json; charset=utf-8');
    res.end(JSON.stringify(state, null, 2));
    return;
  }
  res.setHeader('content-type', 'text/html; charset=utf-8');
  res.end(render());
});

bootstrap().then(() => {
  server.listen(PORT, '0.0.0.0', () => {
    console.log(`Dashboard listening on http://0.0.0.0:${PORT}`);
  });
}).catch((err) => {
  console.error(err && err.stack || err);
  process.exit(1);
});
