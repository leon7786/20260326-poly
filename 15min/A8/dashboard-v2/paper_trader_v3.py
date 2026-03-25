#!/usr/bin/env python3
"""
Polymarket Paper Trader v3.2
- WSS 实时行情：Binance + Coinbase + OKX + Bybit
- WSS 实时盘口：Polymarket CLOB
- REST fallback（WSS 断线 >30s 时降级）
- 0.5 秒轮询（入场窗口内）
- 动态概率模型：胜率 ≥ 80% 才下单
- 主动结算、内存清理、异常兜底
"""
import json
import time
import os
import signal
import sys
import asyncio
import threading
import httpx
import statistics
import websockets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from collections import deque
from typing import Optional, Dict

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

TRADE_LOG = OUTPUT_DIR / "paper_trades_v3.jsonl"
STATE_FILE = DATA_DIR / "paper_state_v3.json"
PID_FILE = OUTPUT_DIR / "paper_trader.pid"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


# ══════════════════════════════════════════════════════════
# WSS 实时行情聚合器
# ══════════════════════════════════════════════════════════

def _ts_log(msg):
    now = datetime.now(timezone.utc) + timedelta(hours=8)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class WSPriceAggregator:
    """通过 WSS 实时获取价格，接口兼容旧版 PriceAggregator"""

    # 币种到各交易所 symbol 的映射
    COIN_MAP = {
        "btc": {"binance": "btcusdt", "coinbase": "BTC-USD", "okx": "BTC-USDT", "bybit": "BTCUSDT"},
        "eth": {"binance": "ethusdt", "coinbase": "ETH-USD", "okx": "ETH-USDT", "bybit": "ETHUSDT"},
        "sol": {"binance": "solusdt", "coinbase": "SOL-USD", "okx": "SOL-USDT", "bybit": "SOLUSDT"},
        "xrp": {"binance": "xrpusdt", "coinbase": "XRP-USD", "okx": "XRP-USDT", "bybit": "XRPUSDT"},
        "doge": {"binance": "dogeusdt", "coinbase": "DOGE-USD", "okx": "DOGE-USDT", "bybit": "DOGEUSDT"},
        "hype": {"binance": "hypeusdt", "coinbase": None, "okx": "HYPE-USDT", "bybit": "HYPEUSDT"},
        "bnb": {"binance": "bnbusdt", "coinbase": None, "okx": "BNB-USDT", "bybit": "BNBUSDT"},
    }

    # REST fallback 配置
    REST_SOURCES = {
        "binance": {
            "url": "https://api.binance.com/api/v3/ticker/price",
            "params_fn": lambda sym: {"symbol": f"{sym}USDT"},
            "parse_fn": lambda r: float(r.json()["price"]),
        },
        "coinbase": {
            "url": "https://api.coinbase.com/v2/prices/{sym}-USD/spot",
            "params_fn": lambda sym: {},
            "parse_fn": lambda r: float(r.json()["data"]["amount"]),
        },
        "okx": {
            "url": "https://www.okx.com/api/v5/market/ticker",
            "params_fn": lambda sym: {"instId": f"{sym}-USDT"},
            "parse_fn": lambda r: float(r.json()["data"][0]["last"]),
        },
        "bybit": {
            "url": "https://api.bybit.com/v5/market/tickers",
            "params_fn": lambda sym: {"category": "spot", "symbol": f"{sym}USDT"},
            "parse_fn": lambda r: float(r.json()["result"]["list"][0]["lastPrice"]),
        },
    }

    def __init__(self, coins=None):
        self.coins = [c.lower() for c in (coins or ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"])]
        self._lock = threading.Lock()

        # 价格存储: {coin: {source: price}}
        self._prices: Dict[str, Dict[str, float]] = {c: {} for c in self.coins}
        # 最后更新时间: {source: timestamp}
        self._last_update: Dict[str, float] = {}
        # 连接状态: {source: bool}
        self._connected: Dict[str, bool] = {
            "binance": False, "coinbase": False, "okx": False, "bybit": False
        }
        # 重连计数
        self._reconnect_count: Dict[str, int] = {s: 0 for s in self._connected}

        # Polymarket 盘口: {token_id: {"bid": float, "ask": float, ...}}
        self._poly_books: Dict[str, dict] = {}
        self._poly_subscribed: set = set()
        self._poly_connected = False
        self._poly_ws = None

        # 兼容旧接口
        self.price_history: Dict[str, deque] = {}
        self.latest_prices: Dict[str, Dict[str, float]] = {}
        self.source_health: Dict[str, bool] = {s: True for s in self._connected}
        self.source_latency: Dict[str, deque] = {s: deque(maxlen=20) for s in self._connected}

        # REST fallback
        self._http = httpx.Client(timeout=3)
        self._rest_fallback_interval = 30  # WSS 断线超过 30s 用 REST

        # 启动 WSS 后台线程
        self._loop = None
        self._thread = threading.Thread(target=self._run_ws_loop, daemon=True)
        self._thread.start()

        # 等待至少 1 个源连接
        for _ in range(50):  # 最多等 5 秒
            if any(self._connected.values()):
                break
            time.sleep(0.1)

    def _run_ws_loop(self):
        """后台线程运行 asyncio event loop"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._ws_manager())

    async def _ws_manager(self):
        """管理所有 WSS 连接"""
        tasks = [
            asyncio.create_task(self._ws_binance()),
            asyncio.create_task(self._ws_coinbase()),
            asyncio.create_task(self._ws_okx()),
            asyncio.create_task(self._ws_bybit()),
            asyncio.create_task(self._ws_polymarket()),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── Binance WSS ──────────────────────────────────────

    async def _ws_binance(self):
        streams = [f"{self.COIN_MAP[c]['binance']}@ticker" for c in self.coins if self.COIN_MAP[c].get("binance")]
        url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"
        await self._ws_connect("binance", url, self._handle_binance)

    def _handle_binance(self, msg):
        try:
            data = json.loads(msg)
            if "data" not in data:
                return
            d = data["data"]
            symbol = d.get("s", "").lower()  # e.g. "BTCUSDT" -> "btcusdt"
            price = float(d.get("c", 0))  # close price
            if price <= 0:
                return
            # 反查 coin
            for coin, mapping in self.COIN_MAP.items():
                if mapping.get("binance") == symbol:
                    self._update_price(coin, "binance", price)
                    break
        except Exception:
            pass

    # ── Coinbase WSS ─────────────────────────────────────

    async def _ws_coinbase(self):
        products = [self.COIN_MAP[c]["coinbase"] for c in self.coins if self.COIN_MAP[c].get("coinbase")]
        if not products:
            return

        async def on_open(ws):
            await ws.send(json.dumps({
                "type": "subscribe",
                "channels": [{"name": "ticker", "product_ids": products}]
            }))

        await self._ws_connect("coinbase", "wss://ws-feed.exchange.coinbase.com", self._handle_coinbase, on_open=on_open)

    def _handle_coinbase(self, msg):
        try:
            data = json.loads(msg)
            if data.get("type") != "ticker":
                return
            product = data.get("product_id", "")
            price = float(data.get("price", 0))
            if price <= 0:
                return
            for coin, mapping in self.COIN_MAP.items():
                if mapping.get("coinbase") == product:
                    self._update_price(coin, "coinbase", price)
                    break
        except Exception:
            pass

    # ── OKX WSS ──────────────────────────────────────────

    async def _ws_okx(self):
        args = [{"channel": "tickers", "instId": self.COIN_MAP[c]["okx"]}
                for c in self.coins if self.COIN_MAP[c].get("okx")]

        async def on_open(ws):
            await ws.send(json.dumps({"op": "subscribe", "args": args}))

        await self._ws_connect("okx", "wss://ws.okx.com:8443/ws/v5/public", self._handle_okx, on_open=on_open)

    def _handle_okx(self, msg):
        try:
            data = json.loads(msg)
            if "data" not in data:
                return
            for item in data["data"]:
                inst_id = item.get("instId", "")
                price = float(item.get("last", 0))
                if price <= 0:
                    continue
                for coin, mapping in self.COIN_MAP.items():
                    if mapping.get("okx") == inst_id:
                        self._update_price(coin, "okx", price)
                        break
        except Exception:
            pass

    # ── Bybit WSS ────────────────────────────────────────

    async def _ws_bybit(self):
        args = [f"tickers.{self.COIN_MAP[c]['bybit']}" for c in self.coins if self.COIN_MAP[c].get("bybit")]

        async def on_open(ws):
            await ws.send(json.dumps({"op": "subscribe", "args": args}))

        await self._ws_connect("bybit", "wss://stream.bybit.com/v5/public/spot", self._handle_bybit, on_open=on_open)

    def _handle_bybit(self, msg):
        try:
            data = json.loads(msg)
            if "data" not in data:
                return
            d = data["data"]
            symbol = d.get("symbol", "")
            price = float(d.get("lastPrice", 0))
            if price <= 0:
                return
            for coin, mapping in self.COIN_MAP.items():
                if mapping.get("bybit") == symbol:
                    self._update_price(coin, "bybit", price)
                    break
        except Exception:
            pass

    # ── Polymarket CLOB WSS ──────────────────────────────

    async def _ws_polymarket(self):
        """Polymarket CLOB orderbook WSS"""
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        backoff = 2
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self._poly_connected = True
                    self._poly_ws = ws
                    _ts_log("🔌 Polymarket CLOB WSS 已连接")
                    backoff = 2

                    # 重新订阅之前的 token
                    for token_id in list(self._poly_subscribed):
                        try:
                            await ws.send(json.dumps({
                                "type": "subscribe",
                                "channel": "market",
                                "assets_ids": [token_id],
                            }))
                        except Exception:
                            pass

                    async for msg in ws:
                        self._handle_polymarket(msg)

            except Exception as e:
                self._poly_connected = False
                self._poly_ws = None
                _ts_log(f"⚠️ Polymarket WSS 断线: {e}, {backoff}s 后重连")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle_polymarket(self, msg):
        """解析 Polymarket orderbook 更新"""
        try:
            data = json.loads(msg)
            # Polymarket CLOB WSS 推送格式可能是:
            # {"market": ..., "asset_id": ..., "bids": [...], "asks": [...]}
            # 或 event-based: {"event_type": "book", ...}
            asset_id = data.get("asset_id") or data.get("market")
            if not asset_id:
                return

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            if bids or asks:
                with self._lock:
                    book = self._poly_books.get(asset_id, {})
                    if bids:
                        # 按价格降序排列
                        sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
                        book["bid"] = float(sorted_bids[0]["price"]) if sorted_bids else 0
                        book["bids"] = sorted_bids[:10]
                    if asks:
                        sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 0)))
                        book["ask"] = float(sorted_asks[0]["price"]) if sorted_asks else 1.0
                        book["asks"] = sorted_asks[:10]
                    book["updated"] = time.time()
                    self._poly_books[asset_id] = book
        except Exception:
            pass

    def subscribe_polymarket(self, token_id: str):
        """动态订阅 Polymarket 市场（线程安全）"""
        if token_id in self._poly_subscribed:
            return
        self._poly_subscribed.add(token_id)
        if self._poly_connected and self._poly_ws and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._poly_ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": "market",
                        "assets_ids": [token_id],
                    })),
                    self._loop
                )
            except Exception:
                pass

    def get_poly_book(self, token_id: str) -> Optional[dict]:
        """获取 Polymarket WSS 盘口数据"""
        with self._lock:
            book = self._poly_books.get(token_id)
            if book and time.time() - book.get("updated", 0) < 30:
                return book
        return None

    # ── 通用 WSS 连接管理 ────────────────────────────────

    async def _ws_connect(self, source: str, url: str, handler, on_open=None):
        """通用 WSS 连接 + 自动重连"""
        backoff = 2
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self._connected[source] = True
                    self._last_update[source] = time.time()
                    _ts_log(f"🔌 {source} WSS 已连接")
                    backoff = 2

                    if on_open:
                        await on_open(ws)

                    async for msg in ws:
                        handler(msg)
                        self._last_update[source] = time.time()

            except Exception as e:
                self._connected[source] = False
                self._reconnect_count[source] += 1
                _ts_log(f"⚠️ {source} WSS 断线: {e}, {backoff}s 后重连 (第{self._reconnect_count[source]}次)")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ── 价格更新（线程安全）────────────────────────────

    def _update_price(self, coin: str, source: str, price: float):
        with self._lock:
            self._prices[coin][source] = price
            self._last_update[source] = time.time()
            self._connected[source] = True
            self.source_health[source] = True

            # 更新历史
            all_prices = dict(self._prices[coin])
            if all_prices:
                median = statistics.median(all_prices.values())
                hist = self.price_history.setdefault(coin, deque(maxlen=500))
                # 限制更新频率：每 0.3 秒最多记录一个点
                if not hist or time.time() - hist[-1]["t"] >= 0.3:
                    hist.append({"t": time.time(), "price": median})
                self.latest_prices[coin] = all_prices

    # ── REST fallback ────────────────────────────────────

    def _rest_fallback(self, coin: str) -> Dict[str, float]:
        """WSS 全断时用 REST 补一次"""
        sym = coin.upper()
        prices = {}
        for name, cfg in self.REST_SOURCES.items():
            # 只对已断线的源做 fallback
            if self._connected.get(name, False):
                continue
            try:
                url = cfg["url"].format(sym=sym)
                params = cfg["params_fn"](sym)
                t0 = time.monotonic()
                r = self._http.get(url, params=params)
                r.raise_for_status()
                price = cfg["parse_fn"](r)
                lat = (time.monotonic() - t0) * 1000
                self.source_latency[name].append(lat)
                prices[name] = price
            except Exception:
                pass
        return prices

    # ── 兼容旧接口 ──────────────────────────────────────

    def get_prices(self, coin: str) -> Dict[str, float]:
        with self._lock:
            prices = dict(self._prices.get(coin.lower(), {}))

        # 检查是否需要 REST fallback
        now = time.time()
        wss_sources_alive = sum(
            1 for s, t in self._last_update.items()
            if s != "polymarket" and now - t < self._rest_fallback_interval
        )
        if wss_sources_alive < 2:
            rest_prices = self._rest_fallback(coin)
            prices.update(rest_prices)

        if prices:
            median = statistics.median(prices.values())
            hist = self.price_history.setdefault(coin.lower(), deque(maxlen=500))
            if not hist or now - hist[-1]["t"] >= 0.3:
                hist.append({"t": now, "price": median})
            self.latest_prices[coin.lower()] = prices

        return prices

    def get_median_price(self, coin: str) -> Optional[float]:
        prices = self.get_prices(coin)
        return statistics.median(prices.values()) if prices else None

    def get_consensus(self, coin: str, open_price: float) -> dict:
        prices = self.latest_prices.get(coin.lower(), {})
        if not prices:
            return {"direction": None, "agree": 0, "total": 0}
        up = sum(1 for p in prices.values() if p > open_price)
        down = sum(1 for p in prices.values() if p < open_price)
        total = len(prices)
        if up > down:
            return {"direction": "UP", "agree": up, "total": total}
        elif down > up:
            return {"direction": "DOWN", "agree": down, "total": total}
        return {"direction": None, "agree": 0, "total": total}

    def compute_rsi(self, coin: str, period: int = 14) -> float:
        hist = self.price_history.get(coin.lower(), deque())
        if len(hist) < period + 1:
            return 50
        prices = [h["price"] for h in hist]
        deltas = [prices[i] - prices[i - 1] for i in range(-period, 0)]
        gains = [d for d in deltas if d > 0]
        losses = [-d for d in deltas if d < 0]
        ag = sum(gains) / period if gains else 0
        al = sum(losses) / period if losses else 0.0001
        return 100 - (100 / (1 + ag / al))

    def get_trend_strength(self, coin: str, lookback: int = 30) -> float:
        hist = self.price_history.get(coin.lower(), deque())
        if len(hist) < lookback:
            return 0
        prices = [h["price"] for h in list(hist)[-lookback:]]
        n = len(prices)
        xm = (n - 1) / 2
        ym = sum(prices) / n
        num = sum((i - xm) * (p - ym) for i, p in enumerate(prices))
        den = sum((i - xm) ** 2 for i in range(n))
        slope = num / den if den else 0
        return (slope / ym * 100) if ym else 0

    def source_status(self) -> str:
        now = time.time()
        connected = []
        for s in ["binance", "coinbase", "okx", "bybit"]:
            last = self._last_update.get(s, 0)
            if self._connected.get(s) and now - last < 30:
                connected.append(s)
        poly_status = "poly:✅" if self._poly_connected else "poly:❌"
        lats = {}
        for s, dq in self.source_latency.items():
            if dq:
                lats[s] = f"{statistics.mean(dq):.0f}ms"
        return f"WSS {len(connected)}/4 [{','.join(connected)}] {poly_status} | REST fallback: {lats if lats else 'idle'}"


# ══════════════════════════════════════════════════════════
# 概率模型（不变）
# ══════════════════════════════════════════════════════════

def estimate_win_probability(momentum_pct, seconds_to_settlement, trend_strength, consensus_ratio, rsi):
    abs_mom = abs(momentum_pct)
    if abs_mom < 0.02:
        base = 0.52
    elif abs_mom < 0.05:
        base = 0.70 + (abs_mom - 0.02) / 0.03 * 0.22
    elif abs_mom < 0.10:
        base = 0.92 + (abs_mom - 0.05) / 0.05 * 0.05
    elif abs_mom < 0.20:
        base = 0.97 + (abs_mom - 0.10) / 0.10 * 0.02
    else:
        base = 0.99
    time_factor = max(0.75, min(1.0, 1.0 - max(0, seconds_to_settlement - 60) / 2000))
    trend_bonus = min(0.03, abs(trend_strength) * 5)
    consensus_bonus = (consensus_ratio - 0.5) * 0.06
    rsi_penalty = 0
    if momentum_pct > 0 and rsi > 75:
        rsi_penalty = (rsi - 75) / 100 * 0.10
    elif momentum_pct < 0 and rsi < 25:
        rsi_penalty = (25 - rsi) / 100 * 0.10
    return max(0.50, min(0.995, base * time_factor + trend_bonus + consensus_bonus - rsi_penalty))


# ══════════════════════════════════════════════════════════
# 配置（不变）
# ══════════════════════════════════════════════════════════

@dataclass
class Config:
    coins: list = None
    min_win_prob: float = 0.80
    entry_window_start: int = 840
    entry_window_end: int = 10
    fast_poll_interval: float = 0.5
    slow_poll_interval: float = 3.0
    max_buy_price: float = 0.70
    strict_real_odds: bool = True
    bet_size: float = 1.0
    max_daily_trades: int = 80
    max_daily_loss: float = -15.0

    def __post_init__(self):
        if self.coins is None:
            self.coins = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]


# ══════════════════════════════════════════════════════════
# 交易引擎
# ══════════════════════════════════════════════════════════

class PaperTraderV3:
    def __init__(self, config=None):
        self.cfg = config or Config()
        self.prices = WSPriceAggregator(coins=self.cfg.coins)
        self.http = httpx.Client(timeout=10)

        self.total_pnl = 0.0
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.today = ""
        self.total_wins = 0
        self.total_count = 0
        self.total_skips = 0

        self.windows = {}
        self._logged_skips = set()
        self._load_state()

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                s = json.loads(STATE_FILE.read_text())
                self.total_pnl = s.get("total_pnl", 0)
                self.total_wins = s.get("total_wins", 0)
                self.total_count = s.get("total_count", 0)
                self.total_skips = s.get("total_skips", 0)
            except Exception:
                pass

    def _save_state(self):
        wr = (self.total_wins / self.total_count * 100) if self.total_count else 0
        STATE_FILE.write_text(json.dumps({
            "total_pnl": round(self.total_pnl, 4),
            "total_wins": self.total_wins,
            "total_count": self.total_count,
            "total_skips": self.total_skips,
            "win_rate": round(wr, 1),
            "daily_pnl": round(self.daily_pnl, 4),
            "daily_trades": self.daily_trades,
            "updated": datetime.now(timezone.utc).isoformat(),
            "sources": self.prices.source_status(),
            "pid": os.getpid(),
        }, indent=2))

    def _log(self, msg):
        _ts_log(msg)

    def _window_ts(self):
        now = datetime.now(timezone.utc)
        ws = (now.minute // 15) * 15
        start = now.replace(minute=ws, second=0, microsecond=0)
        end = start + timedelta(minutes=15)
        return int(start.timestamp()), start, end

    def _get_market(self, coin, start_unix):
        slug = f"{coin}-updown-15m-{start_unix}"
        try:
            r = self.http.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=5)
            data = r.json()
            if not data:
                return None
            m = data[0]
            m["_gamma_prices"] = m.get("outcomePrices")
            m["_gamma_ask"] = m.get("bestAsk")

            cid = m.get("conditionId")
            if cid:
                cache_key = f"_clob_tokens_{cid}"
                if cache_key not in self.windows:
                    try:
                        r2 = self.http.get(f"{CLOB}/markets/{cid}", timeout=5)
                        if r2.status_code == 200:
                            clob_data = r2.json()
                            clob_tokens = clob_data.get("tokens", [])
                            if len(clob_tokens) >= 2:
                                m["_up_token"] = clob_tokens[0]["token_id"]
                                m["_down_token"] = clob_tokens[1]["token_id"]
                                self.windows[cache_key] = {
                                    "up": clob_tokens[0]["token_id"],
                                    "down": clob_tokens[1]["token_id"],
                                }
                                # 动态订阅 Polymarket WSS
                                self.prices.subscribe_polymarket(clob_tokens[0]["token_id"])
                                self.prices.subscribe_polymarket(clob_tokens[1]["token_id"])
                    except Exception:
                        pass
                else:
                    cached = self.windows[cache_key]
                    m["_up_token"] = cached["up"]
                    m["_down_token"] = cached["down"]
            return m
        except Exception:
            return None

    def _get_book(self, token_id):
        """REST fallback 获取盘口"""
        try:
            r = self.http.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=5)
            book = r.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0
            best_ask = float(asks[0]["price"]) if asks else 1.0
            return {
                "bid": best_bid,
                "ask": best_ask,
                "spread": round(best_ask - best_bid, 3) if bids and asks else 1.0,
                "bid_depth": sum(float(b["size"]) * float(b["price"]) for b in bids[:10]),
                "ask_depth": sum(float(a["size"]) * float(a["price"]) for a in asks[:10]),
                "bid_levels": len(bids),
                "ask_levels": len(asks),
                "has_liquidity": bool(bids and asks),
            }
        except Exception:
            return {"bid": 0, "ask": 1, "spread": 1, "bid_depth": 0, "ask_depth": 0,
                    "bid_levels": 0, "ask_levels": 0, "has_liquidity": False}

    def _resolve_buy_price(self, market, direction):
        if not market:
            return None, "none", {}

        token = market.get("_up_token") if direction == "UP" else market.get("_down_token")
        if token:
            # 优先从 WSS 内存读盘口
            ws_book = self.prices.get_poly_book(token)
            if ws_book and "ask" in ws_book:
                ask = ws_book["ask"]
                if 0 < ask < 1:
                    return ask, "wss", {
                        "bid_levels": len(ws_book.get("bids", [])),
                        "ask_levels": len(ws_book.get("asks", [])),
                        "has_liquidity": True,
                    }

            # REST fallback
            book = self._get_book(token)
            if book["has_liquidity"]:
                return book["ask"], "clob", book

        # Gamma fallback
        gp = market.get("_gamma_prices")
        if gp:
            try:
                if isinstance(gp, str):
                    gp = json.loads(gp)
                idx = 0 if direction == "UP" else 1
                price = float(gp[idx])
                if 0.01 < price < 0.99:
                    return price, "gamma", {}
            except Exception:
                pass
        return None, "none", {}

    # ── 主动结算 ─────────────────────────────────────────

    def _settle_all_due(self):
        now = datetime.now(timezone.utc)
        to_settle = []
        for key, w in list(self.windows.items()):
            if not isinstance(w, dict):
                continue
            if not w.get("traded") or w.get("settled"):
                continue
            end_ts = w.get("start_unix", 0) + 900
            if now.timestamp() >= end_ts + 5:
                to_settle.append((key, w))

        for key, w in to_settle:
            coin = key.split("-")[0]
            try:
                close_price = self.prices.get_median_price(coin)
                if close_price is None:
                    continue
                self._do_settle(key, w, coin, close_price)
            except Exception as e:
                self._log(f"⚠️ 结算异常 {key}: {e}")

    def _do_settle(self, key, w, coin, close_price):
        w["settled"] = True
        w["close_price"] = close_price
        direction = w["direction"]
        open_p = w["open_price"]
        buy_p = w["buy_price"]

        won = (close_price > open_p) if direction == "UP" else (close_price < open_p)
        pnl = (1.0 - buy_p) * self.cfg.bet_size if won else -buy_p * self.cfg.bet_size

        w["won"] = won
        w["pnl"] = round(pnl, 4)

        self.total_pnl += pnl
        self.daily_pnl += pnl
        self.daily_trades += 1
        self.total_count += 1
        if won:
            self.total_wins += 1

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "coin": coin, "direction": direction,
            "open": open_p, "entry": w.get("entry_price", 0), "close": close_price,
            "momentum": w.get("momentum", 0), "win_prob": w.get("win_prob", 0),
            "rsi": w.get("rsi", 50), "trend": w.get("trend", 0),
            "consensus": w.get("consensus"),
            "num_sources": w.get("num_sources", 0),
            "buy_price": buy_p, "price_source": w.get("price_source", "?"),
            "secs_before_settle": w.get("secs_left", 0),
            "won": won, "pnl": round(pnl, 4),
            "total_pnl": round(self.total_pnl, 4),
        }
        with open(TRADE_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
        self._save_state()

        icon = "✅" if won else "❌"
        wr = (self.total_wins / self.total_count * 100) if self.total_count else 0
        self._log(
            f"{icon} {coin.upper()} {direction} | ${pnl:+.2f} | prob={w.get('win_prob', 0):.0%} "
            f"| 累计${self.total_pnl:+.2f} WR={wr:.0f}% ({self.total_count}笔)"
        )

    # ── 内存清理 ─────────────────────────────────────────

    def _cleanup_old_windows(self):
        cutoff = time.time() - 1800
        to_del = [k for k, v in self.windows.items()
                  if isinstance(v, dict) and v.get("start_unix", 0) < cutoff]
        for k in to_del:
            del self.windows[k]
        old_skips = [k for k in self._logged_skips if any(
            k.startswith(dk.rsplit("-skip", 1)[0]) for dk in to_del
        )] if to_del else []
        for k in old_skips:
            self._logged_skips.discard(k)

    # ── 入场逻辑 ─────────────────────────────────────────

    def _check_and_trade(self, coin):
        start_unix, start_dt, end_dt = self._window_ts()
        now = datetime.now(timezone.utc)
        secs_left = (end_dt - now).total_seconds()
        window_key = f"{coin}-{start_unix}"

        if window_key not in self.windows:
            price = self.prices.get_median_price(coin)
            if price is None:
                return
            self.windows[window_key] = {
                "start_unix": start_unix,
                "open_price": price,
                "traded": False,
                "settled": False,
            }
            end_local = (end_dt + timedelta(hours=8)).strftime("%H:%M")
            self._log(f"📦 {coin.upper()} 窗口 → {end_local} | 开盘 ${price:,.1f}")

        w = self.windows[window_key]
        if w["traded"] or w["settled"]:
            return
        if secs_left > self.cfg.entry_window_start or secs_left < self.cfg.entry_window_end:
            return

        price = self.prices.get_median_price(coin)
        if price is None:
            return
        momentum = ((price - w["open_price"]) / w["open_price"]) * 100
        if abs(momentum) < 0.02:
            return
        direction = "UP" if momentum > 0 else "DOWN"

        consensus = self.prices.get_consensus(coin, w["open_price"])
        if consensus["total"] == 0:
            return
        consensus_ratio = consensus["agree"] / consensus["total"]
        rsi = self.prices.compute_rsi(coin)
        trend = self.prices.get_trend_strength(coin)

        win_prob = estimate_win_probability(momentum, secs_left, trend, consensus_ratio, rsi)
        if win_prob < self.cfg.min_win_prob:
            return
        if self.daily_trades >= self.cfg.max_daily_trades or self.daily_pnl <= self.cfg.max_daily_loss:
            return

        market = self._get_market(coin, start_unix)
        buy_price, price_source, book_info = self._resolve_buy_price(market, direction)

        # 套利检测
        if market and price_source in ("clob", "wss"):
            opp_dir = "DOWN" if direction == "UP" else "UP"
            opp_token = market.get("_down_token") if direction == "UP" else market.get("_up_token")
            if opp_token:
                opp_book = self.prices.get_poly_book(opp_token)
                opp_ask = None
                if opp_book and "ask" in opp_book:
                    opp_ask = opp_book["ask"]
                else:
                    opp_rest = self._get_book(opp_token)
                    if opp_rest["has_liquidity"]:
                        opp_ask = opp_rest["ask"]
                if opp_ask and buy_price:
                    total_cost = buy_price + opp_ask
                    if total_cost < 0.95:
                        self._log(
                            f"💰 套利信号 {coin.upper()} | {direction}@${buy_price:.2f} + {opp_dir}@${opp_ask:.2f} "
                            f"= ${total_cost:.3f} | 利润 ${1 - total_cost:.3f}/share"
                        )

        if self.cfg.strict_real_odds and price_source == "none":
            sk = f"{window_key}-{coin}-nobook"
            if sk not in self._logged_skips:
                self._logged_skips.add(sk)
                self._log(f"⏭ {coin.upper()} {direction} | prob={win_prob:.0%} mom={momentum:+.3f}% | 无盘口")
            self.total_skips += 1
            return
        if buy_price is not None and buy_price > self.cfg.max_buy_price:
            sk = f"{window_key}-{coin}-expensive"
            if sk not in self._logged_skips:
                self._logged_skips.add(sk)
                self._log(f"⏭ {coin.upper()} {direction} | ask=${buy_price:.2f}({price_source}) > ${self.cfg.max_buy_price:.2f} | book={book_info.get('bid_levels', 0)}b/{book_info.get('ask_levels', 0)}a")
            self.total_skips += 1
            return
        if buy_price is None:
            return

        w["traded"] = True
        w["direction"] = direction
        w["entry_price"] = price
        w["buy_price"] = buy_price
        w["price_source"] = price_source
        w["momentum"] = round(momentum, 4)
        w["win_prob"] = round(win_prob, 4)
        w["consensus"] = consensus
        w["rsi"] = round(rsi, 1)
        w["trend"] = round(trend, 4)
        w["secs_left"] = round(secs_left, 0)
        w["num_sources"] = consensus["total"]

        bl = book_info.get("bid_levels", 0)
        al = book_info.get("ask_levels", 0)
        self._log(
            f"🎯 {coin.upper()} {direction} | prob={win_prob:.0%} mom={momentum:+.3f}% "
            f"| ${buy_price:.2f}({price_source},{bl}b/{al}a) | {consensus['agree']}/{consensus['total']}源 "
            f"| RSI={rsi:.0f} | {secs_left:.0f}s"
        )

    # ── 主循环 ───────────────────────────────────────────

    def run(self):
        cfg = self.cfg

        PID_FILE.write_text(str(os.getpid()))

        def _sigterm(sig, frame):
            self._log(f"收到 SIGTERM，保存状态...")
            self._save_state()
            sys.exit(0)
        signal.signal(signal.SIGTERM, _sigterm)

        print("=" * 65, flush=True)
        print("🤖 Polymarket Paper Trader v3.2 (WSS Multi-Source + Auto-Settle)", flush=True)
        print("=" * 65, flush=True)
        print(f"  PID: {os.getpid()}", flush=True)
        print(f"  币种: {', '.join(c.upper() for c in cfg.coins)}", flush=True)
        print(f"  价格源: Binance + Coinbase + OKX + Bybit (WSS)", flush=True)
        print(f"  盘口源: Polymarket CLOB (WSS + REST fallback)", flush=True)
        print(f"  策略: 胜率 ≥ {cfg.min_win_prob:.0%} | 最大买入 ${cfg.max_buy_price}", flush=True)
        print(f"  入场窗口: 结算前 {cfg.entry_window_start}s ~ {cfg.entry_window_end}s", flush=True)
        print(f"  轮询: 入场 {cfg.fast_poll_interval}s / 空闲 {cfg.slow_poll_interval}s", flush=True)
        print(f"  累计: ${self.total_pnl:+.2f} | {self.total_count}笔", flush=True)
        print(f"  日志: {TRADE_LOG}", flush=True)
        print("=" * 65, flush=True)

        self._log("等待 WSS 连接...")
        # 等待至少 2 个源有数据
        for _ in range(100):
            ready = sum(1 for c in cfg.coins[:3]  # 检查 BTC/ETH/SOL
                        if self.prices.get_prices(c))
            if ready >= 2:
                break
            time.sleep(0.1)

        self._log(f"源状态: {self.prices.source_status()}")
        self._log("开始监控...")

        heartbeat = 0
        err_count = 0

        while True:
            try:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if today != self.today:
                    if self.today and self.daily_trades > 0:
                        self._log(f"📅 日结 | ${self.daily_pnl:+.2f} ({self.daily_trades}笔)")
                    self.today = today
                    self.daily_trades = 0
                    self.daily_pnl = 0

                self._settle_all_due()
                self._cleanup_old_windows()

                _, _, end_dt = self._window_ts()
                secs_left = (end_dt - datetime.now(timezone.utc)).total_seconds()
                in_entry = self.cfg.entry_window_end < secs_left < self.cfg.entry_window_start

                for coin in cfg.coins:
                    try:
                        self._check_and_trade(coin)
                    except Exception as e:
                        self._log(f"⚠️ {coin.upper()} check 异常: {e}")

                heartbeat += 1
                interval = cfg.fast_poll_interval if in_entry else cfg.slow_poll_interval
                beats_per_10m = int(600 / interval)
                if heartbeat % beats_per_10m == 0:
                    wr = (self.total_wins / self.total_count * 100) if self.total_count else 0
                    self._log(
                        f"💓 ${self.total_pnl:+.2f} | {self.total_count}笔 WR={wr:.0f}% "
                        f"| 跳过{self.total_skips} | {self.prices.source_status()}"
                    )
                    self._save_state()

                err_count = 0
                time.sleep(interval)

            except KeyboardInterrupt:
                self._log(f"⏹ 手动停止 | ${self.total_pnl:+.2f} | {self.total_count}笔")
                self._save_state()
                break
            except Exception as e:
                err_count += 1
                self._log(f"❌ 主循环异常 ({err_count}): {e}")
                if err_count > 20:
                    self._log("连续错误过多，等 60 秒...")
                    time.sleep(60)
                    err_count = 0
                else:
                    time.sleep(5)


def show_stats():
    if not TRADE_LOG.exists():
        print("暂无记录")
        return
    trades = [json.loads(l) for l in TRADE_LOG.read_text().splitlines() if l.strip()]
    if not trades:
        print("暂无记录")
        return
    wins = sum(1 for t in trades if t["won"])
    total = len(trades)
    pnl = sum(t["pnl"] for t in trades)
    print(f"\n📊 Paper Trader v3.2 统计")
    print(f"{'=' * 55}")
    print(f"  总交易: {total} | 胜率: {wins / total * 100:.1f}% | PnL: ${pnl:+.2f}")
    by_coin = {}
    for t in trades:
        c = t["coin"]
        by_coin.setdefault(c, {"w": 0, "l": 0, "pnl": 0})
        if t["won"]:
            by_coin[c]["w"] += 1
        else:
            by_coin[c]["l"] += 1
        by_coin[c]["pnl"] += t["pnl"]
    for c, s in by_coin.items():
        tc = s["w"] + s["l"]
        wr = s["w"] / tc * 100 if tc else 0
        print(f"  {c.upper()}: {tc}笔 WR={wr:.0f}% PnL=${s['pnl']:+.2f}")
    print(f"\n  最近 10 笔:")
    for t in trades[-10:]:
        icon = "✅" if t["won"] else "❌"
        ts = t["ts"][11:19]
        print(f"  {icon} {ts} {t['coin'].upper()} {t['direction']} | prob={t.get('win_prob', 0):.0%} ${t['pnl']:+.2f}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        show_stats()
    else:
        trader = PaperTraderV3()
        trader.run()
