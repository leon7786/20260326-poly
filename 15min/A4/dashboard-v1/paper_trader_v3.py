#!/usr/bin/env python3
"""
Polymarket Paper Trader v3.1
- 多价格源：Binance + Coinbase + OKX + Bybit
- 0.5 秒轮询（入场窗口内）
- 动态概率模型：胜率 ≥ 80% 才下单
- 修复：主动结算（不依赖窗口切换）、内存清理、异常兜底
"""
import json
import time
import os
import signal
import sys
import httpx
import statistics
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass
from collections import deque
from typing import Optional, Dict

import websocket

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

TRADE_LOG = OUTPUT_DIR / "paper_trades_v3.jsonl"
ROUND_LOG = OUTPUT_DIR / "paper_rounds_v3.jsonl"
STATE_FILE = DATA_DIR / "paper_state_v3.json"
PID_FILE = OUTPUT_DIR / "paper_trader.pid"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


# ══════════════════════════════════════════════════════════
# 多价格源
# ══════════════════════════════════════════════════════════

class PriceAggregator:
    BINANCE_SUPPORTED = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}
    OKX_SUPPORTED = {"BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB"}

    def __init__(self, coins, on_update=None):
        self.coins = [c.lower() for c in coins]
        self.source_health = {"binance_wss": False, "okx_wss": False}
        self.source_latency = {s: deque(maxlen=200) for s in self.source_health}
        self.last_source_update = {s: 0.0 for s in self.source_health}
        self.price_history: Dict[str, deque] = {c: deque(maxlen=4000) for c in self.coins}
        self.latest_prices: Dict[str, Dict[str, float]] = {c: {} for c in self.coins}
        self.latest_update: Dict[str, Dict[str, float]] = {c: {} for c in self.coins}
        self._last_hist_append = {c: 0.0 for c in self.coins}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ws_apps = {}
        self._threads = []
        self.on_update = on_update
        self._start_threads()

    def _start_threads(self):
        for target, name in [
            (self._binance_loop, "binance-wss"),
            (self._okx_loop, "okx-wss"),
        ]:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        for wsapp in list(self._ws_apps.values()):
            try:
                wsapp.close()
            except Exception:
                pass

    def _set_health(self, source: str, healthy: bool):
        with self._lock:
            self.source_health[source] = healthy

    def _record_latency(self, source: str, event_ms):
        try:
            if event_ms is None:
                return
            event_ms = float(event_ms)
            latency = max(0.0, time.time() * 1000 - event_ms)
            self.source_latency[source].append(latency)
        except Exception:
            pass

    def _append_history(self, coin: str):
        prices = self.get_live_prices(coin)
        if not prices:
            return
        now = time.time()
        if now - self._last_hist_append.get(coin, 0.0) < 0.5:
            return
        median = statistics.median(prices.values())
        self.price_history.setdefault(coin, deque(maxlen=4000)).append({"t": now, "price": median})
        self._last_hist_append[coin] = now

    def _update_price(self, source: str, coin: str, price: float, event_ms=None):
        if coin not in self.latest_prices:
            return
        now_ts = time.time()
        with self._lock:
            self.latest_prices[coin][source] = price
            self.latest_update[coin][source] = now_ts
            self.last_source_update[source] = now_ts
            self._record_latency(source, event_ms)
            self._append_history(coin)
        if self.on_update:
            try:
                self.on_update("underlying", coin, source)
            except Exception:
                pass

    def _binance_loop(self):
        streams = [f"{coin}usdt@bookTicker" for coin in self.coins if coin.upper() in self.BINANCE_SUPPORTED]
        if not streams:
            return
        url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

        while not self._stop.is_set():
            def on_open(wsapp):
                self._set_health("binance_wss", True)

            def on_message(wsapp, message):
                try:
                    payload = json.loads(message)
                    data = payload.get("data", payload)
                    symbol = (data.get("s") or payload.get("stream", "").split("@")[0]).upper()
                    if not symbol.endswith("USDT"):
                        return
                    coin = symbol[:-4].lower()
                    bid = float(data.get("b", 0) or 0)
                    ask = float(data.get("a", 0) or 0)
                    price = (bid + ask) / 2 if bid > 0 and ask > 0 else ask or bid
                    if price <= 0:
                        return
                    self._set_health("binance_wss", True)
                    self._update_price("binance_wss", coin, price, data.get("E") or data.get("T"))
                except Exception:
                    pass

            def on_error(wsapp, error):
                self._set_health("binance_wss", False)

            def on_close(wsapp, close_status_code, close_msg):
                self._set_health("binance_wss", False)

            wsapp = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            self._ws_apps["binance_wss"] = wsapp
            try:
                wsapp.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                self._set_health("binance_wss", False)
            if not self._stop.is_set():
                time.sleep(3)

    def _okx_loop(self):
        args = [{"channel": "tickers", "instId": f"{coin.upper()}-USDT"} for coin in self.coins if coin.upper() in self.OKX_SUPPORTED]
        if not args:
            return
        url = "wss://ws.okx.com:8443/ws/v5/public"

        while not self._stop.is_set():
            def on_open(wsapp):
                self._set_health("okx_wss", True)
                wsapp.send(json.dumps({"op": "subscribe", "args": args}))

            def on_message(wsapp, message):
                try:
                    payload = json.loads(message)
                    if payload.get("event"):
                        if payload.get("event") == "subscribe":
                            self._set_health("okx_wss", True)
                        return
                    arg = payload.get("arg", {})
                    if arg.get("channel") != "tickers":
                        return
                    inst_id = arg.get("instId", "")
                    if not inst_id.endswith("-USDT"):
                        return
                    coin = inst_id.split("-", 1)[0].lower()
                    rows = payload.get("data", [])
                    if not rows:
                        return
                    price = float(rows[0].get("last", 0) or 0)
                    if price <= 0:
                        return
                    self._set_health("okx_wss", True)
                    self._update_price("okx_wss", coin, price, rows[0].get("ts"))
                except Exception:
                    pass

            def on_error(wsapp, error):
                self._set_health("okx_wss", False)

            def on_close(wsapp, close_status_code, close_msg):
                self._set_health("okx_wss", False)

            wsapp = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            self._ws_apps["okx_wss"] = wsapp
            try:
                wsapp.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                self._set_health("okx_wss", False)
            if not self._stop.is_set():
                time.sleep(3)

    def get_live_prices(self, coin: str) -> Dict[str, float]:
        now = time.time()
        with self._lock:
            prices = {}
            for source, price in self.latest_prices.get(coin, {}).items():
                ts = self.latest_update.get(coin, {}).get(source, 0)
                if now - ts <= 15:
                    prices[source] = price
            return prices

    def get_median_price(self, coin: str) -> Optional[float]:
        prices = self.get_live_prices(coin)
        return statistics.median(prices.values()) if prices else None

    def get_consensus(self, coin: str, open_price: float) -> dict:
        prices = self.get_live_prices(coin)
        if not prices:
            return {"direction": None, "agree": 0, "total": 0}
        up = sum(1 for p in prices.values() if p > open_price)
        down = sum(1 for p in prices.values() if p < open_price)
        total = len(prices)
        if up > down:
            return {"direction": "UP", "agree": up, "total": total}
        if down > up:
            return {"direction": "DOWN", "agree": down, "total": total}
        return {"direction": None, "agree": 0, "total": total}

    def compute_rsi(self, coin: str, period: int = 14) -> float:
        hist = self.price_history.get(coin, deque())
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
        hist = self.price_history.get(coin, deque())
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
        healthy = sum(1 for v in self.source_health.values() if v)
        now = time.time()
        ages = {s: f"{int((now - ts) * 1000)}ms" for s, ts in self.last_source_update.items() if ts}
        lats = {s: f"{statistics.mean(dq):.0f}ms" for s, dq in self.source_latency.items() if dq}
        parts = [f"{healthy}/{len(self.source_health)} wss"]
        if ages:
            parts.append(f"age={ages}")
        if lats:
            parts.append(f"lat={lats}")
        return " | ".join(parts)


class PolymarketCLOBClient:
    def __init__(self, coins, on_update=None):
        self.coins = [c.lower() for c in coins]
        self.http = httpx.Client(timeout=10)
        self.source_health = {"polymarket_clob_wss": False}
        self.source_latency = {"polymarket_clob_wss": deque(maxlen=200)}
        self.last_source_update = {"polymarket_clob_wss": 0.0}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ws_app = None
        self.current_start_unix = None
        self.market_meta = {}
        self.asset_map = {}
        self.books = {}
        self.on_update = on_update
        self.thread = threading.Thread(target=self._run_loop, name="polymarket-clob-wss", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._ws_app:
                self._ws_app.close()
        except Exception:
            pass

    def _window_start_unix(self):
        now = datetime.now(timezone.utc)
        ws_min = (now.minute // 15) * 15
        start = now.replace(minute=ws_min, second=0, microsecond=0)
        return int(start.timestamp())

    def _parse_json_field(self, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return value
        return value

    def _refresh_markets(self, start_unix: int):
        market_meta = {}
        asset_map = {}
        for coin in self.coins:
            slug = f"{coin}-updown-15m-{start_unix}"
            try:
                resp = self.http.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=8)
                data = resp.json()
                if not data:
                    continue
                market = data[0]
                clob_ids = self._parse_json_field(market.get("clobTokenIds")) or []
                outcomes = self._parse_json_field(market.get("outcomes")) or ["Up", "Down"]
                if len(clob_ids) < 2:
                    continue
                direction_tokens = {}
                for idx, token in enumerate(clob_ids[:2]):
                    outcome = str(outcomes[idx]).lower() if idx < len(outcomes) else ("up" if idx == 0 else "down")
                    direction = "UP" if "up" in outcome else "DOWN"
                    direction_tokens[direction] = token
                    asset_map[token] = {"coin": coin, "direction": direction}
                market_meta[coin] = {
                    "coin": coin,
                    "slug": slug,
                    "start_unix": start_unix,
                    "question": market.get("question"),
                    "condition_id": market.get("conditionId"),
                    "up_token": direction_tokens.get("UP"),
                    "down_token": direction_tokens.get("DOWN"),
                }
            except Exception:
                continue
        with self._lock:
            self.current_start_unix = start_unix
            self.market_meta = market_meta
            self.asset_map = asset_map
            self.books = {}
        return list(asset_map.keys())

    def _empty_book(self):
        return {
            "bid": 0.0,
            "ask": 0.0,
            "spread": 0.0,
            "bid_depth": 0.0,
            "ask_depth": 0.0,
            "bid_levels": 0,
            "ask_levels": 0,
            "has_liquidity": False,
            "asset_id": None,
            "last_trade_price": 0.0,
            "last_trade_side": None,
            "updated_at": None,
        }

    def _update_snapshot(self, item: dict):
        asset_id = item.get("asset_id")
        if not asset_id:
            return
        bids = item.get("bids") or []
        asks = item.get("asks") or []
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0
        book = {
            "bid": best_bid,
            "ask": best_ask,
            "spread": round(best_ask - best_bid, 6) if best_bid and best_ask else 0.0,
            "bid_depth": sum(float(b["size"]) * float(b["price"]) for b in bids[:10]),
            "ask_depth": sum(float(a["size"]) * float(a["price"]) for a in asks[:10]),
            "bid_levels": len(bids),
            "ask_levels": len(asks),
            "has_liquidity": bool(best_bid and best_ask),
            "asset_id": asset_id,
            "last_trade_price": 0.0,
            "last_trade_side": None,
            "updated_at": item.get("timestamp"),
        }
        now_ts = time.time()
        with self._lock:
            self.books[asset_id] = book
            self.last_source_update["polymarket_clob_wss"] = now_ts
        try:
            ts = float(item.get("timestamp"))
            self.source_latency["polymarket_clob_wss"].append(max(0.0, time.time() * 1000 - ts))
        except Exception:
            pass
        if self.on_update:
            try:
                meta = self.asset_map.get(asset_id, {})
                self.on_update("polymarket", meta.get("coin"), asset_id)
            except Exception:
                pass

    def _update_price_change(self, payload: dict):
        changes = payload.get("price_changes") or []
        ts = payload.get("timestamp")
        for change in changes:
            asset_id = change.get("asset_id")
            if not asset_id:
                continue
            with self._lock:
                book = dict(self.books.get(asset_id, self._empty_book()))
            best_bid = float(change.get("best_bid", book.get("bid", 0)) or 0)
            best_ask = float(change.get("best_ask", book.get("ask", 0)) or 0)
            book.update({
                "bid": best_bid,
                "ask": best_ask,
                "spread": round(best_ask - best_bid, 6) if best_bid and best_ask else 0.0,
                "has_liquidity": bool(best_bid and best_ask),
                "asset_id": asset_id,
                "last_trade_price": float(change.get("price", book.get("last_trade_price", 0)) or 0),
                "last_trade_side": change.get("side"),
                "updated_at": ts,
            })
            with self._lock:
                self.books[asset_id] = book
                self.last_source_update["polymarket_clob_wss"] = time.time()
            if self.on_update:
                try:
                    meta = self.asset_map.get(asset_id, {})
                    self.on_update("polymarket", meta.get("coin"), asset_id)
                except Exception:
                    pass
        try:
            if ts is not None:
                self.source_latency["polymarket_clob_wss"].append(max(0.0, time.time() * 1000 - float(ts)))
        except Exception:
            pass

    def _handle_message(self, message: str):
        try:
            payload = json.loads(message)
        except Exception:
            return
        self.source_health["polymarket_clob_wss"] = True
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    self._update_snapshot(item)
            return
        if isinstance(payload, dict):
            if payload.get("event_type") == "price_change":
                self._update_price_change(payload)
            elif payload.get("asset_id"):
                self._update_snapshot(payload)

    def _run_once(self, start_unix: int, asset_ids):
        if not asset_ids:
            time.sleep(2)
            return
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

        def on_open(wsapp):
            self.source_health["polymarket_clob_wss"] = True
            wsapp.send(json.dumps({"type": "Market", "assets_ids": asset_ids, "auth": {}}))

        def on_message(wsapp, message):
            if self._stop.is_set():
                wsapp.close()
                return
            self._handle_message(message)

        def on_error(wsapp, error):
            self.source_health["polymarket_clob_wss"] = False

        def on_close(wsapp, close_status_code, close_msg):
            self.source_health["polymarket_clob_wss"] = False

        wsapp = websocket.WebSocketApp(
            url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws_app = wsapp
        rotation_seconds = max(5, start_unix + 905 - time.time())
        rotator = threading.Timer(rotation_seconds, wsapp.close)
        rotator.daemon = True
        rotator.start()
        try:
            wsapp.run_forever(ping_interval=20, ping_timeout=10)
        finally:
            rotator.cancel()
            self.source_health["polymarket_clob_wss"] = False

    def _run_loop(self):
        while not self._stop.is_set():
            try:
                start_unix = self._window_start_unix()
                asset_ids = self._refresh_markets(start_unix)
                self._run_once(start_unix, asset_ids)
            except Exception:
                self.source_health["polymarket_clob_wss"] = False
                if not self._stop.is_set():
                    time.sleep(3)

    def get_market(self, coin: str, start_unix: int):
        with self._lock:
            market = self.market_meta.get(coin)
            if market and market.get("start_unix") == start_unix:
                return dict(market)
        return None

    def get_book(self, coin: str, direction: str, start_unix: int):
        market = self.get_market(coin, start_unix)
        if not market:
            return self._empty_book()
        asset_id = market.get("up_token") if direction == "UP" else market.get("down_token")
        with self._lock:
            book = dict(self.books.get(asset_id, self._empty_book()))
        book["asset_id"] = asset_id
        return book

    def source_status(self) -> str:
        healthy = sum(1 for v in self.source_health.values() if v)
        now = time.time()
        ages = {s: f"{int((now - ts) * 1000)}ms" for s, ts in self.last_source_update.items() if ts}
        lats = {s: f"{statistics.mean(dq):.0f}ms" for s, dq in self.source_latency.items() if dq}
        parts = [f"{healthy}/{len(self.source_health)} wss"]
        if ages:
            parts.append(f"age={ages}")
        if lats:
            parts.append(f"lat={lats}")
        return " | ".join(parts)


# ══════════════════════════════════════════════════════════
# 概率模型
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
# 配置
# ══════════════════════════════════════════════════════════

@dataclass
class Config:
    coins: list = None
    # 回归第一版思路：动量 + RSI + 临近结算入场；这里做轻度放宽
    momentum_threshold: float = 0.08
    entry_window_start: int = 150
    entry_window_end: int = 10
    fast_poll_interval: float = 0.5
    slow_poll_interval: float = 3.0
    max_buy_price: float = 0.75
    strict_real_odds: bool = True
    bet_size: float = 1.0
    max_daily_trades: int = 80
    max_daily_loss: float = -15.0
    rsi_overbought: float = 80.0
    rsi_oversold: float = 20.0
    # 概率模型保留用于展示，但默认不再做硬门槛
    min_win_prob: float = 0.76
    use_probability_gate: bool = False

    def __post_init__(self):
        if self.coins is None:
            self.coins = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]


# ══════════════════════════════════════════════════════════
# 交易引擎
# ══════════════════════════════════════════════════════════

class PaperTraderV3:
    def __init__(self, config=None):
        self.cfg = config or Config()
        self._wakeup = threading.Event()
        self.prices = PriceAggregator(self.cfg.coins, on_update=self._on_market_update)
        self.poly = PolymarketCLOBClient(self.cfg.coins, on_update=self._on_market_update)
        self.http = httpx.Client(timeout=10)

        self.total_pnl = 0.0
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.today = ""
        self.total_wins = 0
        self.total_count = 0
        self.total_skips = 0

        # 只保留最近 3 个窗口（防内存泄漏）
        self.windows = {}
        self._logged_skips = set()
        self.round_records = {}
        self._load_state()

    def _on_market_update(self, *_args, **_kwargs):
        self._wakeup.set()

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                s = json.loads(STATE_FILE.read_text())
                self.total_pnl = s.get("total_pnl", 0)
                self.total_wins = s.get("total_wins", 0)
                self.total_count = s.get("total_count", 0)
                self.total_skips = s.get("total_skips", 0)
                self.daily_pnl = s.get("daily_pnl", 0)
                self.daily_trades = s.get("daily_trades", 0)
                self.today = s.get("today", "")
            except Exception:
                pass

    def _save_state(self):
        wr = (self.total_wins / self.total_count * 100) if self.total_count else 0
        STATE_FILE.write_text(json.dumps({
            "initial_bankroll": 7.0,
            "equity": round(7.0 + self.total_pnl, 4),
            "total_pnl": round(self.total_pnl, 4),
            "total_wins": self.total_wins,
            "total_count": self.total_count,
            "total_skips": self.total_skips,
            "win_rate": round(wr, 1),
            "daily_pnl": round(self.daily_pnl, 4),
            "daily_trades": self.daily_trades,
            "today": self.today,
            "updated": datetime.now(timezone.utc).isoformat(),
            "sources": f"underlying={self.prices.source_status()} | polymarket={self.poly.source_status()}",
            "config": {
                "momentum_threshold": self.cfg.momentum_threshold,
                "entry_window_start": self.cfg.entry_window_start,
                "entry_window_end": self.cfg.entry_window_end,
                "min_win_prob": self.cfg.min_win_prob,
                "use_probability_gate": self.cfg.use_probability_gate,
                "max_buy_price": self.cfg.max_buy_price,
                "bet_size": self.cfg.bet_size,
            },
            "pid": os.getpid(),
        }, indent=2))

    def _log(self, msg):
        now = datetime.now(timezone.utc) + timedelta(hours=8)
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    def _window_ts(self):
        now = datetime.now(timezone.utc)
        ws = (now.minute // 15) * 15
        start = now.replace(minute=ws, second=0, microsecond=0)
        end = start + timedelta(minutes=15)
        return int(start.timestamp()), start, end

    def _get_market(self, coin, start_unix):
        """获取 Gamma + CLOB 市场数据，正确解析 token IDs"""
        slug = f"{coin}-updown-15m-{start_unix}"
        try:
            return self.poly.get_market(coin, start_unix)
        except Exception:
            return None

    def _get_book(self, token_id):
        # Legacy fallback no longer used in WSS mode
        return {"bid": 0, "ask": 0, "spread": 0, "bid_depth": 0, "ask_depth": 0,
                "bid_levels": 0, "ask_levels": 0, "has_liquidity": False}

    def _resolve_buy_price(self, market, direction):
        """从 Polymarket CLOB WSS 订单簿获取买入价"""
        if not market:
            return None, "none", {}
        book = self.poly.get_book(market.get("coin"), direction, market.get("start_unix"))
        if book.get("has_liquidity") and book.get("ask", 0) > 0:
            return book["ask"], "clob_wss", book
        return None, "none", book

    # ── 主动结算：不再等窗口切换 ────────────────────────

    def _settle_all_due(self):
        """主动结算所有已过期且有交易的窗口"""
        now = datetime.now(timezone.utc)
        to_settle = []
        for key, w in list(self.windows.items()):
            if not isinstance(w, dict):
                continue
            if not w.get("traded") or w.get("settled"):
                continue
            # 窗口已过期？
            end_ts = w.get("start_unix", 0) + 900  # 15min = 900s
            if now.timestamp() >= end_ts + 5:  # 5 秒缓冲
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

        round_record = self.round_records.get(key, {
            "window_key": key,
            "coin": coin,
            "start_unix": w.get("start_unix"),
            "open_price": round(open_p, 6),
        })
        round_record.update({
            "settle_ts": datetime.now(timezone.utc).isoformat(),
            "close_price": round(close_price, 6),
            "flip_count": int(w.get("flip_count", 0)),
            "flip_times": list(w.get("flip_times", [])),
            "max_abs_momentum_pct": round(w.get("max_abs_momentum", 0.0), 6),
            "won": bool(won),
            "pnl": round(pnl, 4),
            "final_direction_vs_open": "UP" if close_price > open_p else "DOWN" if close_price < open_p else "FLAT",
            "trade_taken": True,
        })
        with open(ROUND_LOG, "a") as f:
            f.write(json.dumps(round_record) + "\n")
        self.round_records.pop(key, None)

        self._save_state()

        icon = "✅" if won else "❌"
        wr = (self.total_wins / self.total_count * 100) if self.total_count else 0
        self._log(
            f"{icon} {coin.upper()} {direction} | ${pnl:+.2f} | prob={w.get('win_prob', 0):.0%} "
            f"| 累计${self.total_pnl:+.2f} WR={wr:.0f}% ({self.total_count}笔)"
        )

    # ── 内存清理 ─────────────────────────────────────────

    def _cleanup_old_windows(self):
        """清理 >30 分钟前的窗口数据"""
        cutoff = time.time() - 1800
        to_del = [k for k, v in self.windows.items()
                  if isinstance(v, dict) and v.get("start_unix", 0) < cutoff]
        for k in to_del:
            w = self.windows[k]
            # 无交易回合也落盘，方便后续全样本回测与学习
            if k not in self.round_records:
                self.round_records[k] = {
                    "window_key": k,
                    "coin": k.split("-")[0],
                    "start_unix": w.get("start_unix"),
                    "open_price": round(w.get("open_price", 0), 6),
                    "trade_taken": False,
                    "flip_count": int(w.get("flip_count", 0)),
                    "flip_times": list(w.get("flip_times", [])),
                    "max_abs_momentum_pct": round(w.get("max_abs_momentum", 0.0), 6),
                    "entry_side": None,
                }
            rr = self.round_records.pop(k, None)
            if rr:
                rr.setdefault("closed_without_trade", True)
                rr.setdefault("settle_ts", datetime.now(timezone.utc).isoformat())
                with open(ROUND_LOG, "a") as f:
                    f.write(json.dumps(rr) + "\n")
            del self.windows[k]
        # 也清理 skip 标记
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
                "flip_count": 0,
                "flip_times": [],
                "last_side_vs_open": None,
                "max_abs_momentum": 0.0,
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

        # 记录每回合相对开盘价的翻转次数与最大偏离，便于日后回测/学习
        current_side = "UP" if momentum > 0 else "DOWN" if momentum < 0 else "FLAT"
        last_side = w.get("last_side_vs_open")
        if last_side and current_side in {"UP", "DOWN"} and last_side in {"UP", "DOWN"} and current_side != last_side:
            w["flip_count"] = w.get("flip_count", 0) + 1
            w.setdefault("flip_times", []).append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "secs_left": round(secs_left, 3),
                "from": last_side,
                "to": current_side,
                "price": round(price, 6),
                "momentum_pct": round(momentum, 6),
            })
        if current_side in {"UP", "DOWN"}:
            w["last_side_vs_open"] = current_side
        w["max_abs_momentum"] = max(abs(momentum), w.get("max_abs_momentum", 0.0))

        if abs(momentum) < self.cfg.momentum_threshold:
            return
        direction = "UP" if momentum > 0 else "DOWN"

        consensus = self.prices.get_consensus(coin, w["open_price"])
        if consensus["total"] == 0:
            return
        consensus_ratio = consensus["agree"] / consensus["total"]
        rsi = self.prices.compute_rsi(coin)
        trend = self.prices.get_trend_strength(coin)

        skip_reason = None

        # 第一版风格：RSI 只做极端过滤，避免过度抑制交易
        if direction == "UP" and rsi > self.cfg.rsi_overbought:
            skip_reason = "rsi_overbought"
        elif direction == "DOWN" and rsi < self.cfg.rsi_oversold:
            skip_reason = "rsi_oversold"

        win_prob = estimate_win_probability(momentum, secs_left, trend, consensus_ratio, rsi)
        if not skip_reason and self.cfg.use_probability_gate and win_prob < self.cfg.min_win_prob:
            skip_reason = "win_prob_too_low"
        if not skip_reason and self.daily_trades >= self.cfg.max_daily_trades:
            skip_reason = "daily_trade_limit"
        if not skip_reason and self.daily_pnl <= self.cfg.max_daily_loss:
            skip_reason = "daily_loss_limit"
        if skip_reason:
            rr = self.round_records.get(window_key) or {
                "window_key": window_key,
                "coin": coin,
                "start_unix": start_unix,
                "start_time_utc": start_dt.isoformat(),
                "end_time_utc": end_dt.isoformat(),
                "open_price": round(w["open_price"], 6),
            }
            rr.update({
                "trade_taken": False,
                "candidate_side": direction,
                "candidate_price": round(price, 6),
                "candidate_momentum_pct": round(momentum, 6),
                "candidate_rsi": round(rsi, 4),
                "candidate_trend": round(trend, 6),
                "candidate_win_prob": round(win_prob, 6),
                "consensus_agree": consensus.get("agree", 0),
                "consensus_total": consensus.get("total", 0),
                "flip_count": int(w.get("flip_count", 0)),
                "flip_times": list(w.get("flip_times", [])),
                "max_abs_momentum_pct": round(w.get("max_abs_momentum", 0.0), 6),
                "skip_reason": skip_reason,
            })
            self.round_records[window_key] = rr
            return

        market = self._get_market(coin, start_unix)
        buy_price, price_source, book_info = self._resolve_buy_price(market, direction)

        # 策略 B：检查反向 token 是否有便宜的 ask（sum-to-less-than-one）
        if market and price_source == "clob_wss":
            opp_dir = "DOWN" if direction == "UP" else "UP"
            opp_book = self.poly.get_book(market.get("coin"), opp_dir, market.get("start_unix"))
            if opp_book["has_liquidity"]:
                total_cost = buy_price + opp_book["ask"]
                if total_cost < 0.95:  # 5% spread = arbitrage
                    self._log(
                        f"💰 套利信号 {coin.upper()} | {direction}@${buy_price:.2f} + {opp_dir}@${opp_book['ask']:.2f} "
                        f"= ${total_cost:.3f} | 利润 ${1-total_cost:.3f}/share"
                    )

        if self.cfg.strict_real_odds and price_source == "none":
            sk = f"{window_key}-{coin}-nobook"
            if sk not in self._logged_skips:
                self._logged_skips.add(sk)
                self._log(f"⏭ {coin.upper()} {direction} | prob={win_prob:.0%} mom={momentum:+.3f}% | 无盘口")
            self.total_skips += 1
            rr = self.round_records.get(window_key) or {}
            rr.update({
                "trade_taken": False,
                "skip_reason": "no_orderbook",
                "candidate_side": direction,
                "candidate_price": round(price, 6),
                "candidate_momentum_pct": round(momentum, 6),
                "candidate_rsi": round(rsi, 4),
                "candidate_trend": round(trend, 6),
                "candidate_win_prob": round(win_prob, 6),
                "consensus_agree": consensus.get("agree", 0),
                "consensus_total": consensus.get("total", 0),
                "flip_count": int(w.get("flip_count", 0)),
                "flip_times": list(w.get("flip_times", [])),
                "max_abs_momentum_pct": round(w.get("max_abs_momentum", 0.0), 6),
            })
            self.round_records[window_key] = rr
            return
        if buy_price is not None and buy_price > self.cfg.max_buy_price:
            sk = f"{window_key}-{coin}-expensive"
            if sk not in self._logged_skips:
                self._logged_skips.add(sk)
                self._log(f"⏭ {coin.upper()} {direction} | ask=${buy_price:.2f}({price_source}) > ${self.cfg.max_buy_price:.2f} | book={book_info.get('bid_levels',0)}b/{book_info.get('ask_levels',0)}a")
            self.total_skips += 1
            rr = self.round_records.get(window_key) or {}
            rr.update({
                "trade_taken": False,
                "skip_reason": "price_too_expensive",
                "candidate_side": direction,
                "candidate_price": round(price, 6),
                "candidate_momentum_pct": round(momentum, 6),
                "candidate_rsi": round(rsi, 4),
                "candidate_trend": round(trend, 6),
                "candidate_win_prob": round(win_prob, 6),
                "consensus_agree": consensus.get("agree", 0),
                "consensus_total": consensus.get("total", 0),
                "flip_count": int(w.get("flip_count", 0)),
                "flip_times": list(w.get("flip_times", [])),
                "max_abs_momentum_pct": round(w.get("max_abs_momentum", 0.0), 6),
                "buy_price": round(buy_price, 6),
                "price_source": price_source,
                "bid": round(book_info.get("bid", 0), 6) if book_info else 0,
                "ask": round(book_info.get("ask", 0), 6) if book_info else 0,
                "spread": round(book_info.get("spread", 0), 6) if book_info else 0,
                "bid_depth": round(book_info.get("bid_depth", 0), 6) if book_info else 0,
                "ask_depth": round(book_info.get("ask_depth", 0), 6) if book_info else 0,
                "bid_levels": int(book_info.get("bid_levels", 0)) if book_info else 0,
                "ask_levels": int(book_info.get("ask_levels", 0)) if book_info else 0,
            })
            self.round_records[window_key] = rr
            return
        if buy_price is None:
            rr = self.round_records.get(window_key) or {}
            rr.update({
                "trade_taken": False,
                "skip_reason": "buy_price_missing",
                "candidate_side": direction,
                "candidate_price": round(price, 6),
                "candidate_momentum_pct": round(momentum, 6),
                "candidate_rsi": round(rsi, 4),
                "candidate_trend": round(trend, 6),
                "candidate_win_prob": round(win_prob, 6),
                "consensus_agree": consensus.get("agree", 0),
                "consensus_total": consensus.get("total", 0),
                "flip_count": int(w.get("flip_count", 0)),
                "flip_times": list(w.get("flip_times", [])),
                "max_abs_momentum_pct": round(w.get("max_abs_momentum", 0.0), 6),
            })
            self.round_records[window_key] = rr
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
        w["book_info"] = book_info

        self.round_records[window_key] = {
            "window_key": window_key,
            "coin": coin,
            "start_unix": start_unix,
            "start_time_utc": start_dt.isoformat(),
            "end_time_utc": end_dt.isoformat(),
            "open_price": round(w["open_price"], 6),
            "entry_price": round(price, 6),
            "entry_side": direction,
            "entry_secs_left": round(secs_left, 0),
            "entry_momentum_pct": round(momentum, 6),
            "entry_rsi": round(rsi, 4),
            "entry_trend": round(trend, 6),
            "entry_win_prob": round(win_prob, 6),
            "consensus_agree": consensus.get("agree", 0),
            "consensus_total": consensus.get("total", 0),
            "buy_price": round(buy_price, 6),
            "price_source": price_source,
            "bid": round(book_info.get("bid", 0), 6) if book_info else 0,
            "ask": round(book_info.get("ask", 0), 6) if book_info else 0,
            "spread": round(book_info.get("spread", 0), 6) if book_info else 0,
            "bid_depth": round(book_info.get("bid_depth", 0), 6) if book_info else 0,
            "ask_depth": round(book_info.get("ask_depth", 0), 6) if book_info else 0,
            "bid_levels": int(book_info.get("bid_levels", 0)) if book_info else 0,
            "ask_levels": int(book_info.get("ask_levels", 0)) if book_info else 0,
            "flip_count": int(w.get("flip_count", 0)),
            "flip_times": list(w.get("flip_times", [])),
            "max_abs_momentum_pct": round(w.get("max_abs_momentum", 0.0), 6),
            "config_snapshot": {
                "momentum_threshold": self.cfg.momentum_threshold,
                "entry_window_start": self.cfg.entry_window_start,
                "entry_window_end": self.cfg.entry_window_end,
                "max_buy_price": self.cfg.max_buy_price,
                "rsi_overbought": self.cfg.rsi_overbought,
                "rsi_oversold": self.cfg.rsi_oversold,
                "bet_size": self.cfg.bet_size,
                "use_probability_gate": self.cfg.use_probability_gate,
                "min_win_prob": self.cfg.min_win_prob,
            },
        }

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

        # 写 PID
        PID_FILE.write_text(str(os.getpid()))

        # 优雅退出
        def _sigterm(sig, frame):
            self._log(f"收到 SIGTERM，保存状态...")
            self.prices.stop()
            self.poly.stop()
            self._save_state()
            sys.exit(0)
        signal.signal(signal.SIGTERM, _sigterm)

        print("=" * 65, flush=True)
        print("🤖 Polymarket Paper Trader v3.1 (Multi-Source + Auto-Settle)", flush=True)
        print("=" * 65, flush=True)
        print(f"  PID: {os.getpid()}", flush=True)
        print(f"  币种: {', '.join(c.upper() for c in cfg.coins)}", flush=True)
        print(f"  价格源: Binance WSS + OKX WSS + Polymarket CLOB WSS", flush=True)
        gate_txt = f"胜率门槛 ≥ {cfg.min_win_prob:.0%}" if cfg.use_probability_gate else "胜率门槛关闭(仅展示)"
        print(f"  策略: 动量 ≥ {cfg.momentum_threshold:.2f}% | {gate_txt} | 最大买入 ${cfg.max_buy_price}", flush=True)
        print(f"  入场窗口: 结算前 {cfg.entry_window_start}s ~ {cfg.entry_window_end}s", flush=True)
        print(f"  触发: WSS event-driven | 最大空闲等待 入场{cfg.fast_poll_interval}s / 空闲{cfg.slow_poll_interval}s", flush=True)
        print(f"  累计: ${self.total_pnl:+.2f} | {self.total_count}笔", flush=True)
        print(f"  日志: {TRADE_LOG}", flush=True)
        print("=" * 65, flush=True)

        self._log("预热 WSS 数据源...")
        for coin in cfg.coins:
            for _ in range(3):
                try:
                    self.prices.get_median_price(coin)
                except Exception:
                    pass
                time.sleep(0.3)
        self._log(f"源状态: underlying={self.prices.source_status()} | polymarket={self.poly.source_status()}")
        self._save_state()
        self._log("开始监控...")

        heartbeat = 0
        err_count = 0

        while True:
            try:
                self._wakeup.clear()
                # 日切换
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if today != self.today:
                    if self.today and self.daily_trades > 0:
                        self._log(f"📅 日结 | ${self.daily_pnl:+.2f} ({self.daily_trades}笔)")
                    self.today = today
                    self.daily_trades = 0
                    self.daily_pnl = 0

                # 主动结算过期窗口（核心修复）
                self._settle_all_due()

                # 清理旧数据（防内存泄漏）
                self._cleanup_old_windows()

                # 入场检查
                _, _, end_dt = self._window_ts()
                secs_left = (end_dt - datetime.now(timezone.utc)).total_seconds()
                in_entry = self.cfg.entry_window_end < secs_left < self.cfg.entry_window_start

                for coin in cfg.coins:
                    try:
                        self._check_and_trade(coin)
                    except Exception as e:
                        self._log(f"⚠️ {coin.upper()} check 异常: {e}")

                # 心跳（每 ~10 分钟）
                heartbeat += 1
                interval = cfg.fast_poll_interval if in_entry else cfg.slow_poll_interval
                beats_per_10m = int(600 / interval)
                if heartbeat % beats_per_10m == 0:
                    wr = (self.total_wins / self.total_count * 100) if self.total_count else 0
                    self._log(
                        f"💓 ${self.total_pnl:+.2f} | {self.total_count}笔 WR={wr:.0f}% "
                        f"| 跳过{self.total_skips} | underlying={self.prices.source_status()} | polymarket={self.poly.source_status()}"
                    )
                    self._save_state()

                err_count = 0  # 重置连续错误计数
                self._wakeup.wait(timeout=interval)

            except KeyboardInterrupt:
                self._log(f"⏹ 手动停止 | ${self.total_pnl:+.2f} | {self.total_count}笔")
                self.prices.stop()
                self.poly.stop()
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
    print(f"\n📊 Paper Trader v3.1 统计")
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
