#!/usr/bin/env python3
"""
Polymarket 15min 实时看板 — 端口 5011
7 币种监控 + 多策略信号 + 纸交易记录
"""
import json
import time
import threading
import statistics
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import deque
from typing import Dict, Optional
import httpx

BASE_DIR = Path(__file__).parent.parent
TRADE_LOG = BASE_DIR / "output" / "paper_trades_v3.jsonl"
STATE_FILE = BASE_DIR / "data" / "paper_state_v3.json"

# ═══════════════════════════════════════════════════
# 7 币种配置
# ═══════════════════════════════════════════════════

COINS = {
    "btc": {"name": "Bitcoin", "icon": "₿", "color": "#f7931a",
            "binance": "BTCUSDT", "coinbase": "BTC", "okx": "BTC-USDT", "bybit": "BTCUSDT"},
    "eth": {"name": "Ethereum", "icon": "Ξ", "color": "#627eea",
            "binance": "ETHUSDT", "coinbase": "ETH", "okx": "ETH-USDT", "bybit": "ETHUSDT"},
    "sol": {"name": "Solana", "icon": "◎", "color": "#9945ff",
            "binance": "SOLUSDT", "coinbase": "SOL", "okx": "SOL-USDT", "bybit": "SOLUSDT"},
    "xrp": {"name": "XRP", "icon": "✕", "color": "#00aae4",
            "binance": "XRPUSDT", "coinbase": "XRP", "okx": "XRP-USDT", "bybit": "XRPUSDT"},
    "doge": {"name": "Dogecoin", "icon": "Ð", "color": "#c2a633",
             "binance": "DOGEUSDT", "coinbase": "DOGE", "okx": "DOGE-USDT", "bybit": "DOGEUSDT"},
    "hype": {"name": "HYPE", "icon": "H", "color": "#00d4aa",
             "binance": "HYPEUSDT", "coinbase": None, "okx": "HYPE-USDT", "bybit": "HYPEUSDT"},
    "bnb": {"name": "BNB", "icon": "◆", "color": "#f3ba2f",
            "binance": "BNBUSDT", "coinbase": None, "okx": "BNB-USDT", "bybit": "BNBUSDT"},
}

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


# ═══════════════════════════════════════════════════
# 数据引擎（后台线程）
# ═══════════════════════════════════════════════════

class DataEngine:
    def __init__(self):
        self.http = httpx.Client(timeout=5)
        self.data: Dict[str, dict] = {}  # coin -> latest data
        self.price_history: Dict[str, deque] = {}  # coin -> deque of prices
        self.lock = threading.Lock()
        self.running = True

    def _window_ts(self):
        now = datetime.now(timezone.utc)
        ws = (now.minute // 15) * 15
        start = now.replace(minute=ws, second=0, microsecond=0)
        end = start + timedelta(minutes=15)
        return int(start.timestamp()), start, end

    def _fetch_binance(self, symbol):
        try:
            r = self.http.get("https://api.binance.com/api/v3/ticker/price",
                              params={"symbol": symbol}, timeout=3)
            return float(r.json()["price"])
        except: return None

    def _fetch_coinbase(self, coin):
        try:
            r = self.http.get(f"https://api.coinbase.com/v2/prices/{coin}-USD/spot", timeout=3)
            return float(r.json()["data"]["amount"])
        except: return None

    def _fetch_okx(self, inst_id):
        try:
            r = self.http.get("https://www.okx.com/api/v5/market/ticker",
                              params={"instId": inst_id}, timeout=3)
            return float(r.json()["data"][0]["last"])
        except: return None

    def _fetch_bybit(self, symbol):
        try:
            r = self.http.get("https://api.bybit.com/v5/market/tickers",
                              params={"category": "spot", "symbol": symbol}, timeout=3)
            return float(r.json()["result"]["list"][0]["lastPrice"])
        except: return None

    def _get_prices(self, coin):
        cfg = COINS[coin]
        prices = {}
        if cfg.get("binance"):
            p = self._fetch_binance(cfg["binance"])
            if p: prices["binance"] = p
        if cfg.get("coinbase"):
            p = self._fetch_coinbase(cfg["coinbase"])
            if p: prices["coinbase"] = p
        if cfg.get("okx"):
            p = self._fetch_okx(cfg["okx"])
            if p: prices["okx"] = p
        if cfg.get("bybit"):
            p = self._fetch_bybit(cfg["bybit"])
            if p: prices["bybit"] = p
        return prices

    def _fetch_polymarket(self, coin, start_unix):
        slug = f"{coin}-updown-15m-{start_unix}"
        try:
            r = self.http.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=5)
            data = r.json()
            if not data:
                return None
            m = data[0]
            result = {
                "volume": m.get("volume", 0),
                "liquidity": m.get("liquidity", 0),
                "bestBid": m.get("bestBid"),
                "bestAsk": m.get("bestAsk"),
                "outcomePrices": m.get("outcomePrices"),
                "lastTradePrice": m.get("lastTradePrice"),
                "active": m.get("active", False),
                "closed": m.get("closed", False),
            }

            # 从 CLOB 获取正确的 token 和真实 book depth
            cid = m.get("conditionId")
            if cid:
                cache_key = f"clob_tokens_{coin}_{start_unix}"
                if cache_key not in self.data:
                    try:
                        r2 = self.http.get(f"{CLOB}/markets/{cid}", timeout=5)
                        if r2.status_code == 200:
                            clob_data = r2.json()
                            tokens = clob_data.get("tokens", [])
                            if len(tokens) >= 2:
                                self.data[cache_key] = {
                                    "up_token": tokens[0]["token_id"],
                                    "down_token": tokens[1]["token_id"],
                                }
                    except:
                        pass

                cached = self.data.get(cache_key, {})
                if cached:
                    # Get real book depth
                    for side, tok_key in [("up", "up_token"), ("down", "down_token")]:
                        try:
                            r3 = self.http.get(f"{CLOB}/book",
                                               params={"token_id": cached[tok_key]}, timeout=3)
                            bk = r3.json()
                            bids = bk.get("bids", [])
                            asks = bk.get("asks", [])
                            best_bid = float(bids[0]["price"]) if bids else 0
                            best_ask = float(asks[0]["price"]) if asks else 0
                            result[f"{side}_bid"] = best_bid
                            result[f"{side}_ask"] = best_ask
                            result[f"{side}_bid_levels"] = len(bids)
                            result[f"{side}_ask_levels"] = len(asks)
                            result[f"{side}_bid_depth"] = round(sum(
                                float(b["size"]) * float(b["price"]) for b in bids[:10]))
                            result[f"{side}_ask_depth"] = round(sum(
                                float(a["size"]) * float(a["price"]) for a in asks[:10]))
                        except:
                            pass

            return result
        except:
            return None

    def _compute_rsi(self, coin, period=14):
        hist = self.price_history.get(coin, deque())
        if len(hist) < period + 1:
            return 50
        prices = [h["p"] for h in list(hist)[-period-1:]]
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [d for d in deltas if d > 0]
        losses = [-d for d in deltas if d < 0]
        ag = sum(gains)/period if gains else 0
        al = sum(losses)/period if losses else 0.0001
        return round(100 - (100 / (1 + ag/al)), 1)

    def _compute_strategy(self, coin, d):
        """多策略信号生成"""
        momentum = d.get("momentum", 0)
        rsi = d.get("rsi", 50)
        secs_left = d.get("secs_left", 900)
        consensus = d.get("consensus", 0)
        total_sources = d.get("total_sources", 0)
        poly_up = d.get("poly_up_price", 0.5)
        poly_down = d.get("poly_down_price", 0.5)

        signals = []
        abs_mom = abs(momentum)

        # ── 策略 1：动量趋势（原策略增强版）
        if abs_mom >= 0.05:
            direction = "UP" if momentum > 0 else "DOWN"
            # 基础概率
            if abs_mom >= 0.20: base = 0.99
            elif abs_mom >= 0.10: base = 0.97
            elif abs_mom >= 0.05: base = 0.92
            else: base = 0.80
            # 时间加成
            time_bonus = max(0, (300 - secs_left) / 300 * 0.05)
            # 共识加成
            cons_bonus = (consensus / total_sources - 0.5) * 0.06 if total_sources else 0
            prob = min(0.995, base + time_bonus + cons_bonus)
            # 买入价 = 对应方向的 Poly 价格
            buy_price = poly_up if direction == "UP" else poly_down
            edge = (prob - buy_price) * 100 if buy_price else 0
            signals.append({
                "name": "动量趋势",
                "direction": direction,
                "prob": round(prob * 100, 1),
                "buy_price": buy_price,
                "edge": round(edge, 1),
                "strength": "强" if abs_mom >= 0.15 else "中" if abs_mom >= 0.08 else "弱",
                "actionable": prob >= 0.80 and buy_price and buy_price <= 0.70,
            })

        # ── 策略 2：高确定性扫单（结算前 3 分钟 + 大动量）
        if secs_left <= 180 and abs_mom >= 0.10:
            direction = "UP" if momentum > 0 else "DOWN"
            prob = min(0.995, 0.95 + abs_mom * 0.5)
            buy_price = poly_up if direction == "UP" else poly_down
            edge = (prob - buy_price) * 100 if buy_price else 0
            signals.append({
                "name": "高确定性扫单",
                "direction": direction,
                "prob": round(prob * 100, 1),
                "buy_price": buy_price,
                "edge": round(edge, 1),
                "strength": "强",
                "actionable": buy_price and buy_price <= 0.85 and edge > 5,
            })

        # ── 策略 3：多源共识（所有源同意 + 动量中等）
        if total_sources >= 3 and consensus == total_sources and abs_mom >= 0.03:
            direction = "UP" if momentum > 0 else "DOWN"
            prob = min(0.99, 0.85 + abs_mom * 2)
            buy_price = poly_up if direction == "UP" else poly_down
            edge = (prob - buy_price) * 100 if buy_price else 0
            signals.append({
                "name": "全源共识",
                "direction": direction,
                "prob": round(prob * 100, 1),
                "buy_price": buy_price,
                "edge": round(edge, 1),
                "strength": "强" if total_sources >= 4 else "中",
                "actionable": prob >= 0.85 and buy_price and buy_price <= 0.65,
            })

        # ── 策略 4：RSI 极端 + 方向确认
        if (rsi > 75 and momentum > 0.05) or (rsi < 25 and momentum < -0.05):
            direction = "UP" if momentum > 0 else "DOWN"
            prob = min(0.99, 0.88 + abs_mom * 1.5)
            buy_price = poly_up if direction == "UP" else poly_down
            edge = (prob - buy_price) * 100 if buy_price else 0
            signals.append({
                "name": "RSI极端确认",
                "direction": direction,
                "prob": round(prob * 100, 1),
                "buy_price": buy_price,
                "edge": round(edge, 1),
                "strength": "中",
                "actionable": prob >= 0.85 and buy_price and buy_price <= 0.70,
            })

        # ── 策略 5：Polymarket 定价偏离（市场价 vs 模型价差 > 10%）
        if abs_mom >= 0.05:
            direction = "UP" if momentum > 0 else "DOWN"
            model_prob = 0.90 + abs_mom * 0.5 if abs_mom >= 0.10 else 0.80 + abs_mom * 2
            model_prob = min(0.99, model_prob)
            market_price = poly_up if direction == "UP" else poly_down
            if market_price and market_price > 0.01 and market_price < 0.99:
                mispricing = (model_prob - market_price) * 100
                if mispricing > 10:
                    signals.append({
                        "name": "定价偏离",
                        "direction": direction,
                        "prob": round(model_prob * 100, 1),
                        "buy_price": market_price,
                        "edge": round(mispricing, 1),
                        "strength": "强" if mispricing > 20 else "中",
                        "actionable": market_price <= 0.70 and mispricing > 10,
                    })

        return signals

    def update_loop(self):
        while self.running:
            try:
                start_unix, start_dt, end_dt = self._window_ts()
                now = datetime.now(timezone.utc)
                secs_left = (end_dt - now).total_seconds()
                secs_elapsed = 900 - secs_left

                for coin in COINS:
                    try:
                        # 多源价格
                        prices = self._get_prices(coin)
                        if not prices:
                            continue
                        median = statistics.median(prices.values())

                        # 历史
                        hist = self.price_history.setdefault(coin, deque(maxlen=500))
                        hist.append({"t": time.time(), "p": median})

                        # 窗口开盘价
                        window_key = f"{coin}-{start_unix}"
                        with self.lock:
                            if window_key not in self.data or not self.data.get(f"{window_key}-open"):
                                self.data[f"{window_key}-open"] = median

                        open_price = self.data.get(f"{window_key}-open", median)
                        momentum = ((median - open_price) / open_price) * 100

                        # 共识
                        up_count = sum(1 for p in prices.values() if p > open_price)
                        down_count = sum(1 for p in prices.values() if p < open_price)
                        consensus = max(up_count, down_count)

                        # RSI
                        rsi = self._compute_rsi(coin)

                        # Polymarket
                        poly = self._fetch_polymarket(coin, start_unix)
                        poly_up = 0.5
                        poly_down = 0.5
                        if poly and poly.get("outcomePrices"):
                            try:
                                ps = poly["outcomePrices"]
                                if isinstance(ps, str):
                                    ps = json.loads(ps)
                                poly_up = float(ps[0])
                                poly_down = float(ps[1])
                            except: pass

                        # CLOB book data
                        up_bid = poly.get("up_bid", 0) if poly else 0
                        up_ask = poly.get("up_ask", 0) if poly else 0
                        down_bid = poly.get("down_bid", 0) if poly else 0
                        down_ask = poly.get("down_ask", 0) if poly else 0

                        d = {
                            "coin": coin,
                            "name": COINS[coin]["name"],
                            "icon": COINS[coin]["icon"],
                            "color": COINS[coin]["color"],
                            "price": median,
                            "prices": prices,
                            "open_price": open_price,
                            "momentum": round(momentum, 4),
                            "rsi": rsi,
                            "secs_left": round(secs_left),
                            "secs_elapsed": round(secs_elapsed),
                            "consensus": consensus,
                            "total_sources": len(prices),
                            "poly_up_price": poly_up,
                            "poly_down_price": poly_down,
                            "poly_volume": poly.get("volume", 0) if poly else 0,
                            "poly_liquidity": poly.get("liquidity", 0) if poly else 0,
                            "poly_active": poly.get("active", False) if poly else False,
                            "poly_bid": poly.get("bestBid") if poly else None,
                            "poly_ask": poly.get("bestAsk") if poly else None,
                            # 真实 CLOB book
                            "up_bid": up_bid, "up_ask": up_ask,
                            "down_bid": down_bid, "down_ask": down_ask,
                            "up_book": f"{poly.get('up_bid_levels',0) if poly else 0}b/{poly.get('up_ask_levels',0) if poly else 0}a",
                            "down_book": f"{poly.get('down_bid_levels',0) if poly else 0}b/{poly.get('down_ask_levels',0) if poly else 0}a",
                            "up_depth": poly.get("up_bid_depth", 0) if poly else 0,
                            "down_depth": poly.get("down_bid_depth", 0) if poly else 0,
                            "arb_spread": round(1 - (up_ask + down_ask), 3) if up_ask and down_ask else 0,
                            "window_end": (end_dt + timedelta(hours=8)).strftime("%H:%M"),
                            "updated": datetime.now(timezone.utc).isoformat(),
                        }

                        # 策略信号
                        d["signals"] = self._compute_strategy(coin, d)

                        with self.lock:
                            self.data[coin] = d

                    except Exception as e:
                        pass  # 单币种失败不影响其他

                time.sleep(2)  # 看板刷新间隔

            except Exception as e:
                time.sleep(5)

    def get_snapshot(self):
        with self.lock:
            coins_data = {}
            for coin in COINS:
                if coin in self.data:
                    coins_data[coin] = self.data[coin]
            return coins_data

    def get_trades(self):
        try:
            if TRADE_LOG.exists():
                lines = TRADE_LOG.read_text().strip().splitlines()
                return [json.loads(l) for l in lines[-50:] if l.strip()]
        except: pass
        return []

    def get_state(self):
        try:
            if STATE_FILE.exists():
                return json.loads(STATE_FILE.read_text())
        except: pass
        return {}


engine = DataEngine()


# ═══════════════════════════════════════════════════
# Web 服务
# ═══════════════════════════════════════════════════

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket 15min 实时看板</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎱</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#FAF9F7; color:#2D2B28; font-family:'Inter','Helvetica Neue',sans-serif; font-size:13px; }
.wrapper { max-width:66.67%; margin:0 auto; padding:0 20px; }
@media (max-width:1200px) { .wrapper { max-width:90%; } }
@media (max-width:768px) { .wrapper { max-width:100%; padding:0 12px; } }
.header { background:#FDFCFB; padding:20px 0; border-bottom:1px solid #E8E4DF; }
.header-inner { display:flex; justify-content:space-between; align-items:center; }
.header h1 { font-size:20px; font-weight:700; color:#1A1915; letter-spacing:-0.3px; }
.header .stats { color:#8C8680; font-size:12px; font-family:'JetBrains Mono',monospace; }
.header .stats .profit { color:#2E7D32; font-weight:600; }
.header .stats .loss { color:#C62828; font-weight:600; }
.top-market-board { padding:18px 0 14px; }
.section-title { color:#6B5B4E; margin-bottom:12px; font-size:15px; font-weight:600; }
.market-strip { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:10px; }
@media (max-width:1400px) { .market-strip { grid-template-columns:repeat(4,minmax(0,1fr)); } }
@media (max-width:900px) { .market-strip { grid-template-columns:repeat(2,minmax(0,1fr)); } }
.mini-card { background:#FFFFFF; border:1px solid #E8E4DF; border-radius:12px; padding:10px 12px; transition:box-shadow 0.3s,border-color 0.3s; }
.mini-card:hover { box-shadow:0 4px 16px rgba(0,0,0,0.05); border-color:#D4CFC8; }
.mini-top { display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }
.mini-coin { display:flex; align-items:center; gap:8px; font-weight:600; color:#1A1915; }
.mini-price { font-size:15px; font-weight:700; font-family:'JetBrains Mono',monospace; color:#1A1915; }
.mini-sub { display:flex; justify-content:space-between; align-items:center; margin-top:6px; font-size:11px; color:#8C8680; font-family:'JetBrains Mono',monospace; }
.mini-odds { display:flex; gap:6px; margin-top:8px; }
.mini-pill { flex:1; border-radius:8px; padding:5px 6px; text-align:center; font-size:11px; font-family:'JetBrains Mono',monospace; }
.mini-pill.up-bg { background:#F0F7F0; color:#2E7D32; border:1px solid #C8E6C9; }
.mini-pill.down-bg { background:#FFF5F5; color:#C62828; border:1px solid #FFCDD2; }
.layout { display:grid; grid-template-columns:minmax(0,1.9fr) minmax(320px,0.95fr); gap:18px; padding:8px 0 20px; align-items:start; }
@media (max-width:1200px) { .layout { grid-template-columns:1fr; } }
.left-col, .right-col { display:flex; flex-direction:column; gap:18px; }
.grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; padding:0; }
@media (max-width:900px) { .grid { grid-template-columns:1fr; } }
.panel { background:#FFFFFF; border:1px solid #E8E4DF; border-radius:12px; padding:16px; }
.card { background:#FFFFFF; border:1px solid #E8E4DF; border-radius:12px; overflow:hidden; transition:box-shadow 0.3s,border-color 0.3s; }
.card:hover { box-shadow:0 4px 16px rgba(0,0,0,0.06); border-color:#D4CFC8; }
.card-header { padding:12px 16px; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #F0ECE8; }
.card-header .coin-info { display:flex; align-items:center; gap:10px; }
.card-header .coin-icon { font-size:22px; width:30px; text-align:center; }
.card-header .coin-name { font-weight:600; font-size:15px; color:#1A1915; }
.card-header .price { font-size:17px; font-weight:700; color:#1A1915; font-family:'JetBrains Mono',monospace; }
.card-body { padding:12px 16px; }
.row { display:flex; justify-content:space-between; margin-bottom:7px; }
.row .label { color:#8C8680; font-size:12px; }
.row .value { font-weight:500; font-family:'JetBrains Mono',monospace; font-size:12px; }
.metrics-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px 12px; margin-top:10px; }
.metric { background:#FCFBF9; border:1px solid #F0ECE8; border-radius:8px; padding:8px 10px; }
.metric .m-label { color:#8C8680; font-size:11px; margin-bottom:4px; }
.metric .m-value { color:#1A1915; font-size:12px; font-family:'JetBrains Mono',monospace; font-weight:600; }
.momentum-bar { height:5px; background:#EDE9E4; border-radius:3px; margin:10px 0; position:relative; overflow:hidden; }
.momentum-fill { height:100%; border-radius:3px; transition:width 0.5s; }
.up { color:#2E7D32; }
.down { color:#C62828; }
.neutral { color:#8C8680; }
.signals { margin-top:10px; border-top:1px solid #F0ECE8; padding-top:10px; }
.signal { background:#F7F5F2; border-radius:8px; padding:8px 10px; margin-bottom:5px; display:flex; justify-content:space-between; align-items:center; font-size:11px; }
.signal.actionable { border-left:3px solid #2E7D32; background:#F0F7F0; }
.signal.inactive { border-left:3px solid #C4BFB8; opacity:0.65; }
.signal .sig-name { font-weight:600; color:#1A1915; }
.signal .sig-dir { padding:2px 8px; border-radius:4px; font-weight:600; font-size:10px; }
.signal .sig-dir.up-bg { background:#E8F5E9; color:#2E7D32; }
.signal .sig-dir.down-bg { background:#FFEBEE; color:#C62828; }
.poly-row { display:flex; gap:10px; margin:8px 0; }
.poly-side { flex:1; padding:8px 10px; border-radius:8px; text-align:center; }
.poly-up { background:#F0F7F0; border:1px solid #C8E6C9; }
.poly-down { background:#FFF5F5; border:1px solid #FFCDD2; }
.timer { text-align:center; font-size:22px; font-weight:700; margin:8px 0; color:#1A1915; font-family:'JetBrains Mono',monospace; }
.timer.urgent { color:#C62828; animation:pulse 1s infinite; }
@keyframes pulse { 50% { opacity:0.4; } }
.source-dots { display:flex; gap:4px; align-items:center; }
.dot { width:7px; height:7px; border-radius:50%; }
.dot.on { background:#66BB6A; }
.dot.off { background:#C4BFB8; }
.trades-section { padding:0; }
.trades-section h3 { color:#6B5B4E; margin-bottom:10px; font-size:15px; font-weight:600; }
.trade-row { display:flex; justify-content:space-between; padding:6px 10px; border-bottom:1px solid #F0ECE8; font-size:11px; font-family:'JetBrains Mono',monospace; }
.trade-row:hover { background:#F7F5F2; }
.footer { text-align:center; padding:14px 0; color:#B0A99F; font-size:11px; border-top:1px solid #E8E4DF; margin-top:10px; }
.badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:10px; font-weight:600; }
.badge-live { background:#E8F5E9; color:#2E7D32; }
.badge-dead { background:#FFEBEE; color:#C62828; }
.strategies-panel { padding:0; border-top:none; }
.strategies-panel h3 { color:#6B5B4E; margin-bottom:14px; font-size:15px; font-weight:600; }
.strat-grid { display:grid; grid-template-columns:1fr; gap:12px; }
.strat-card { background:#FFFFFF; border:1px solid #E8E4DF; border-radius:10px; padding:14px 16px; transition:box-shadow 0.3s; }
.strat-card:hover { box-shadow:0 2px 12px rgba(0,0,0,0.05); }
.strat-header { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
.strat-num { font-size:16px; color:#8C8680; }
.strat-title { font-weight:600; font-size:14px; color:#1A1915; }
.strat-badge { padding:2px 8px; border-radius:4px; font-size:10px; font-weight:600; }
.strat-core { background:#E8F0FE; color:#1565C0; }
.strat-aggr { background:#FFF3E0; color:#E65100; }
.strat-safe { background:#E8F5E9; color:#2E7D32; }
.strat-desc { color:#5D5852; font-size:12px; line-height:1.5; margin-bottom:8px; }
.strat-params { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }
.strat-params span { background:#F7F5F2; padding:3px 8px; border-radius:4px; font-size:10px; color:#6B5B4E; font-family:'JetBrains Mono',monospace; }
.strat-status { font-size:11px; color:#B0A99F; font-family:'JetBrains Mono',monospace; }
.strat-status.active { color:#2E7D32; font-weight:600; }
.strat-status.fired { color:#E65100; font-weight:600; }
</style>
</head>
<body>
<div class="header">
  <div class="wrapper header-inner">
    <div>
      <h1>🔮 Polymarket 15min 实时看板</h1>
    </div>
    <div class="stats" id="header-stats">加载中...</div>
  </div>
</div>
<div class="wrapper">
  <div class="top-market-board">
    <div class="section-title">📈 行情总览</div>
    <div class="market-strip" id="market-strip"></div>
  </div>

  <div class="layout">
    <div class="left-col">
      <div>
        <div class="section-title">🧭 详细行情</div>
        <div class="grid" id="grid"></div>
      </div>
    </div>

    <div class="right-col">
      <div class="strategies-panel panel">
        <h3>📐 当前策略组</h3>
        <div class="strat-grid">
      <div class="strat-card">
        <div class="strat-header">
          <span class="strat-num">①</span>
          <span class="strat-title">动量趋势</span>
          <span class="strat-badge strat-core">核心</span>
        </div>
        <div class="strat-desc">窗口内价格动量 ≥ 0.05%，多源价格确认方向。动量越大+时间越近结算 → 概率越高。</div>
        <div class="strat-params">
          <span>触发: 动量 ≥ 0.05%</span>
          <span>入场: 胜率 ≥ 80%</span>
          <span>买入: ≤ $0.70</span>
        </div>
        <div class="strat-status" id="strat1-status">待触发</div>
      </div>
      <div class="strat-card">
        <div class="strat-header">
          <span class="strat-num">②</span>
          <span class="strat-title">高确定性扫单</span>
          <span class="strat-badge strat-aggr">激进</span>
        </div>
        <div class="strat-desc">结算前 3 分钟，大动量（≥0.10%）几乎确定方向，快速扫单吃掉剩余的低价 token。</div>
        <div class="strat-params">
          <span>触发: ≤ 180s + 动量 ≥ 0.10%</span>
          <span>入场: 胜率 ≥ 95%</span>
          <span>买入: ≤ $0.85 且 edge > 5%</span>
        </div>
        <div class="strat-status" id="strat2-status">待触发</div>
      </div>
      <div class="strat-card">
        <div class="strat-header">
          <span class="strat-num">③</span>
          <span class="strat-title">全源共识</span>
          <span class="strat-badge strat-safe">稳健</span>
        </div>
        <div class="strat-desc">所有价格源（3-4个交易所）一致同意涨/跌方向，信号可靠性最高。</div>
        <div class="strat-params">
          <span>触发: ≥ 3 源 100% 共识</span>
          <span>入场: 胜率 ≥ 85%</span>
          <span>买入: ≤ $0.65</span>
        </div>
        <div class="strat-status" id="strat3-status">待触发</div>
      </div>
      <div class="strat-card">
        <div class="strat-header">
          <span class="strat-num">④</span>
          <span class="strat-title">RSI 极端确认</span>
          <span class="strat-badge strat-core">核心</span>
        </div>
        <div class="strat-desc">RSI 超买（>75）+价格涨、或 RSI 超卖（<25）+价格跌，动量和技术指标双重确认。</div>
        <div class="strat-params">
          <span>触发: RSI > 75 或 < 25</span>
          <span>入场: 胜率 ≥ 85%</span>
          <span>买入: ≤ $0.70</span>
        </div>
        <div class="strat-status" id="strat4-status">待触发</div>
      </div>
      <div class="strat-card">
        <div class="strat-header">
          <span class="strat-num">⑤</span>
          <span class="strat-title">定价偏离</span>
          <span class="strat-badge strat-aggr">激进</span>
        </div>
        <div class="strat-desc">模型估算的胜率 vs Polymarket 实际赔率差距 >10%，说明市场定价滞后，存在 edge。</div>
        <div class="strat-params">
          <span>触发: 偏离 > 10%</span>
          <span>入场: 买入 ≤ $0.70</span>
          <span>依赖: Polymarket 有盘口</span>
        </div>
        <div class="strat-status" id="strat5-status">待触发</div>
      </div>
        </div>
      </div>

      <div class="trades-section panel" id="trades-section">
        <h3>📋 最近交易记录</h3>
        <div id="trades-list">加载中...</div>
      </div>
    </div>
  </div>
</div>
<div class="footer">
  <div class="wrapper">Paper Trader v3.1 · 4 价格源 · 5 策略 · 7 币种 · 自动刷新 2s</div>
</div>
<script>
function fmt(n, d=2) { return n != null ? Number(n).toFixed(d) : '-'; }
function fmtK(n) { return n >= 1000 ? (n/1000).toFixed(1)+'K' : fmt(n,0); }

function renderMiniCard(coin, d) {
  const mom = d.momentum || 0;
  const momClass = mom > 0.02 ? 'up' : mom < -0.02 ? 'down' : 'neutral';
  const sl = d.secs_left || 0;
  const min = Math.floor(sl/60);
  const sec = Math.floor(sl%60);
  const pu = d.poly_up_price || 0.5;
  const pd = d.poly_down_price || 0.5;
  return `<div class="mini-card">
    <div class="mini-top">
      <div class="mini-coin"><span style="color:${d.color};font-size:18px">${d.icon}</span><span>${(d.coin||coin).toUpperCase()}</span></div>
      <span class="badge ${d.poly_active ? 'badge-live' : 'badge-dead'}">${d.poly_active ? 'LIVE' : 'OFF'}</span>
    </div>
    <div class="mini-price">$${fmt(d.price, d.price > 100 ? 1 : d.price > 1 ? 3 : 5)}</div>
    <div class="mini-odds">
      <div class="mini-pill up-bg">↑ ${fmt(pu)}</div>
      <div class="mini-pill down-bg">↓ ${fmt(pd)}</div>
    </div>
    <div class="mini-sub">
      <span class="${momClass}">${mom > 0 ? '▲' : mom < 0 ? '▼' : '—'} ${fmt(Math.abs(mom),3)}%</span>
      <span>${min}:${String(sec).padStart(2,'0')}</span>
    </div>
  </div>`;
}

function renderCard(coin, d) {
  const mom = d.momentum || 0;
  const momClass = mom > 0.02 ? 'up' : mom < -0.02 ? 'down' : 'neutral';
  const momDir = mom > 0 ? '▲' : mom < 0 ? '▼' : '—';
  const sl = d.secs_left || 0;
  const min = Math.floor(sl/60);
  const sec = Math.floor(sl%60);
  const timerClass = sl < 120 ? 'timer urgent' : 'timer';
  const momBar = Math.min(100, Math.abs(mom) / 0.3 * 100);
  const momColor = mom > 0 ? '#2E7D32' : '#C62828';

  // 源状态
  const sources = d.prices || {};
  const srcNames = ['binance','coinbase','okx','bybit'];
  const dots = srcNames.map(s =>
    `<div class="dot ${sources[s] ? 'on' : 'off'}" title="${s}: ${sources[s] ? '$'+fmt(sources[s]) : 'offline'}"></div>`
  ).join('');

  // Poly 价格
  const pu = d.poly_up_price || 0.5;
  const pd = d.poly_down_price || 0.5;
  const active = d.poly_active;

  // 信号
  let sigHtml = '';
  if (d.signals && d.signals.length > 0) {
    sigHtml = '<div class="signals">';
    for (const s of d.signals) {
      const cls = s.actionable ? 'signal actionable' : 'signal inactive';
      const dirCls = s.direction === 'UP' ? 'sig-dir up-bg' : 'sig-dir down-bg';
      sigHtml += `<div class="${cls}">
        <span><span class="sig-name">${s.name}</span> <span class="${dirCls}">${s.direction}</span></span>
        <span>${s.prob}% | edge ${s.edge}% | ${s.strength}</span>
      </div>`;
    }
    sigHtml += '</div>';
  }

  return `<div class="card">
    <div class="card-header">
      <div class="coin-info">
        <span class="coin-icon" style="color:${d.color}">${d.icon}</span>
        <span class="coin-name">${d.name}</span>
        <span class="badge ${active ? 'badge-live' : 'badge-dead'}">${active ? 'LIVE' : 'OFF'}</span>
      </div>
      <span class="price">$${fmt(d.price, d.price > 100 ? 1 : d.price > 1 ? 3 : 5)}</span>
    </div>
    <div class="card-body">
      <div class="${timerClass}">⏱ ${min}:${String(sec).padStart(2,'0')} <span style="font-size:12px;color:#8C8680">→ ${d.window_end||'?'}</span></div>
      <div class="poly-row">
        <div class="poly-side poly-up">
          <div class="up" style="font-size:18px;font-weight:bold">↑ $${fmt(pu)}</div>
          <div style="font-size:10px;color:#8C8680">CLOB: ${d.up_bid?fmt(d.up_bid):'—'}/${d.up_ask?fmt(d.up_ask):'—'} (${d.up_book||'?'})</div>
        </div>
        <div class="poly-side poly-down">
          <div class="down" style="font-size:18px;font-weight:bold">↓ $${fmt(pd)}</div>
          <div style="font-size:10px;color:#8C8680">CLOB: ${d.down_bid?fmt(d.down_bid):'—'}/${d.down_ask?fmt(d.down_ask):'—'} (${d.down_book||'?'})</div>
        </div>
      </div>
      ${d.arb_spread > 0.03 ? '<div style="text-align:center;color:#2E7D32;font-weight:bold;font-size:12px">💰 套利空间: $'+fmt(d.arb_spread,3)+'/share</div>' : ''}
      <div class="row">
        <span class="label">动量</span>
        <span class="value ${momClass}">${momDir} ${fmt(Math.abs(mom),3)}%</span>
      </div>
      <div class="momentum-bar">
        <div class="momentum-fill" style="width:${momBar}%;background:${momColor}"></div>
      </div>
      <div class="metrics-grid">
        <div class="metric">
          <div class="m-label">RSI(14)</div>
          <div class="m-value" style="color:${d.rsi>70?'#C62828':d.rsi<30?'#2E7D32':'#2D2B28'}">${fmt(d.rsi,1)}</div>
        </div>
        <div class="metric">
          <div class="m-label">开盘价</div>
          <div class="m-value">$${fmt(d.open_price, d.open_price>100?1:3)}</div>
        </div>
        <div class="metric">
          <div class="m-label">共识</div>
          <div class="m-value">${d.consensus||0}/${d.total_sources||0} 源</div>
        </div>
        <div class="metric">
          <div class="m-label">流动性</div>
          <div class="m-value">$${fmtK(d.poly_liquidity||0)}</div>
        </div>
        <div class="metric">
          <div class="m-label">成交量</div>
          <div class="m-value">$${fmtK(d.poly_volume||0)}</div>
        </div>
        <div class="metric">
          <div class="m-label">价格源</div>
          <div class="m-value"><span class="source-dots">${dots}</span></div>
        </div>
      </div>
      ${sigHtml}
    </div>
  </div>`;
}

function renderTrades(trades) {
  if (!trades || trades.length === 0) return '<div style="color:#484f58">暂无记录</div>';
  return trades.slice(-20).reverse().map(t => {
    const icon = t.won ? '✅' : '❌';
    const utcMs = Date.parse(t.ts);
    const gmt8Ms = utcMs + 8*3600000;
    const hh = Math.floor((gmt8Ms % 86400000) / 3600000);
    const mm = Math.floor((gmt8Ms % 3600000) / 60000);
    const ss = Math.floor((gmt8Ms % 60000) / 1000);
    const ts = String(hh).padStart(2,'0')+':'+String(mm).padStart(2,'0')+':'+String(ss).padStart(2,'0');
    const pnlCls = t.pnl >= 0 ? 'up' : 'down';
    return `<div class="trade-row">
      <span>${icon} ${ts} ${(t.coin||'').toUpperCase()} ${t.direction}</span>
      <span>prob=${((t.win_prob||0)*100).toFixed(0)}% | src=${t.price_source||'?'}</span>
      <span class="${pnlCls}">$${fmt(t.pnl)}</span>
    </div>`;
  }).join('');
}

async function refresh() {
  try {
    const r = await fetch('/api/data');
    const data = await r.json();

    // Header
    const st = data.state || {};
    const pnl = st.total_pnl || 0;
    const cls = pnl >= 0 ? 'profit' : 'loss';
    const now = new Date();
    const gmt8 = new Date(now.getTime() + (8 - (-now.getTimezoneOffset()/60)) * 3600000);
    const timeStr = gmt8.toTimeString().substring(0,8);
    document.getElementById('header-stats').innerHTML =
      `🕐 ${timeStr} GMT+8 | ` +
      `PnL: <span class="${cls}">$${fmt(pnl)}</span> | ` +
      `${st.total_count||0}笔 WR=${fmt(st.win_rate||0,1)}% | ` +
      `跳过${st.total_skips||0} | ` +
      `${st.sources||'?'}`;

    const coins = data.coins || {};

    // Top market strip
    const marketStrip = document.getElementById('market-strip');
    let topHtml = '';
    for (const [coin, d] of Object.entries(coins)) {
      topHtml += renderMiniCard(coin, d);
    }
    marketStrip.innerHTML = topHtml || '<div style="padding:20px;color:#8C8680">等待数据...</div>';

    // Detailed cards
    const grid = document.getElementById('grid');
    let html = '';
    for (const [coin, d] of Object.entries(coins)) {
      html += renderCard(coin, d);
    }
    grid.innerHTML = html || '<div style="padding:40px;text-align:center;color:#484f58">等待数据...</div>';

    // Trades
    document.getElementById('trades-list').innerHTML = renderTrades(data.trades);

    // Strategy status
    const stratMap = {'动量趋势':1,'高确定性扫单':2,'全源共识':3,'RSI极端确认':4,'定价偏离':5};
    const stratCounts = {1:0,2:0,3:0,4:0,5:0};
    const stratCoins = {1:[],2:[],3:[],4:[],5:[]};
    for (const [coin, d] of Object.entries(coins)) {
      for (const s of (d.signals||[])) {
        const idx = stratMap[s.name];
        if (idx) {
          stratCounts[idx]++;
          stratCoins[idx].push(coin.toUpperCase() + (s.actionable?' ✓':''));
        }
      }
    }
    for (let i=1; i<=5; i++) {
      const el = document.getElementById('strat'+i+'-status');
      if (el) {
        if (stratCounts[i] > 0) {
          el.className = 'strat-status fired';
          el.textContent = '🔥 触发 ' + stratCounts[i] + ' 次 — ' + stratCoins[i].join(', ');
        } else {
          el.className = 'strat-status';
          el.textContent = '⏳ 待触发';
        }
      }
    }

  } catch(e) {
    console.error(e);
  }
}

// 页面可见时才刷新，离开标签页暂停
let _refreshTimer = null;
function startRefresh() {
  if (!_refreshTimer) {
    refresh();
    _refreshTimer = setInterval(refresh, 2000);
  }
}
function stopRefresh() {
  if (_refreshTimer) {
    clearInterval(_refreshTimer);
    _refreshTimer = null;
  }
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopRefresh();
  } else {
    startRefresh();
  }
});
startRefresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默日志

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path == '/api/data':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            data = {
                "coins": engine.get_snapshot(),
                "trades": engine.get_trades(),
                "state": engine.get_state(),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            self.wfile.write(json.dumps(data, default=str).encode())
        else:
            self.send_response(404)
            self.end_headers()


def main():
    port = 5011
    # 启动数据引擎线程
    t = threading.Thread(target=engine.update_loop, daemon=True)
    t.start()
    print(f"🔮 Polymarket Dashboard starting on http://0.0.0.0:{port}", flush=True)
    print(f"   监控: {', '.join(COINS[c]['name'] for c in COINS)}", flush=True)
    server = HTTPServer(('0.0.0.0', port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        engine.running = False
        print("\n⏹ Dashboard stopped", flush=True)


if __name__ == '__main__':
    main()
