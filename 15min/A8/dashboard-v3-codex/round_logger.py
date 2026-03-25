#!/usr/bin/env python3
"""
15min 窗口全量记录器 — 每轮 7 币种完整数据
用于策略自我学习和翻转概率分析

记录内容：
- 每秒价格快照（4 源）
- 窗口内各时间点的方向 & 动量
- 结算后回溯：最后 N 秒的翻转概率
- Polymarket 赔率变化
- 策略信号回顾
"""
import json
import time
import os
import signal
import sys
import httpx
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict, deque
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).parent.parent
LOG_DIR = BASE_DIR / "output" / "rounds"
LOG_DIR.mkdir(parents=True, exist_ok=True)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

COINS = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]

PRICE_SOURCES = {
    "binance": {
        "url": "https://api.binance.com/api/v3/ticker/price",
        "params": lambda sym: {"symbol": f"{sym}USDT"},
        "parse": lambda r: float(r.json()["price"]),
    },
    "coinbase": {
        "url": "https://api.coinbase.com/v2/prices/{sym}-USD/spot",
        "params": lambda sym: {},
        "parse": lambda r: float(r.json()["data"]["amount"]),
    },
    "okx": {
        "url": "https://www.okx.com/api/v5/market/ticker",
        "params": lambda sym: {"instId": f"{sym}-USDT"},
        "parse": lambda r: float(r.json()["data"][0]["last"]),
    },
    "mexc": {
        "url": "https://api.mexc.com/api/v3/ticker/price",
        "params": lambda sym: {"symbol": f"{sym}USDT"},
        "parse": lambda r: float(r.json()["price"]),
    },
}


class RoundLogger:
    def __init__(self):
        self.http = httpx.Client(timeout=3)
        self.running = True
        self.token_cache = {}
        signal.signal(signal.SIGTERM, lambda s, f: self._stop())
        signal.signal(signal.SIGINT, lambda s, f: self._stop())

    def _stop(self):
        self.running = False

    def _log(self, msg):
        now = datetime.now(timezone.utc) + timedelta(hours=8)
        print(f"[{now.strftime('%H:%M:%S')}] {msg}", flush=True)

    def _window_info(self):
        now = datetime.now(timezone.utc)
        ws = (now.minute // 15) * 15
        start = now.replace(minute=ws, second=0, microsecond=0)
        end = start + timedelta(minutes=15)
        return int(start.timestamp()), start, end

    def _get_price(self, coin: str, source: str) -> Optional[float]:
        cfg = PRICE_SOURCES[source]
        sym = coin.upper()
        try:
            url = cfg["url"].format(sym=sym)
            params = cfg["params"](sym)
            r = self.http.get(url, params=params, timeout=2)
            return cfg["parse"](r)
        except:
            return None

    def _get_all_prices(self, coin: str) -> Dict[str, float]:
        prices = {}
        for src in PRICE_SOURCES:
            p = self._get_price(coin, src)
            if p is not None:
                prices[src] = p
        return prices

    def _get_poly_data(self, coin: str, start_unix: int) -> dict:
        slug = f"{coin}-updown-15m-{start_unix}"
        try:
            r = self.http.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=5)
            data = r.json()
            if not data:
                return {}
            m = data[0]
            op = m.get("outcomePrices", [])
            if isinstance(op, str):
                op = json.loads(op)
            up_p = float(op[0]) if len(op) > 0 else 0.5
            down_p = float(op[1]) if len(op) > 1 else 0.5

            result = {
                "up": up_p, "down": down_p,
                "volume": m.get("volume", 0),
                "liquidity": m.get("liquidity", 0),
                "bestBid": m.get("bestBid"),
                "bestAsk": m.get("bestAsk"),
                "lastTrade": m.get("lastTradePrice"),
                "active": m.get("active", False),
            }

            # CLOB market + orderbook features
            cid = m.get("conditionId")
            if cid:
                try:
                    if cid not in self.token_cache:
                        r2 = self.http.get(f"{CLOB}/markets/{cid}", timeout=3)
                        if r2.status_code == 200:
                            market = r2.json()
                            tokens = market.get("tokens", [])
                            if len(tokens) >= 2:
                                self.token_cache[cid] = {
                                    "up": tokens[0]["token_id"],
                                    "down": tokens[1]["token_id"],
                                    "maker_fee": market.get("maker_base_fee"),
                                    "taker_fee": market.get("taker_base_fee"),
                                    "min_order_size": market.get("minimum_order_size"),
                                    "tick_size": market.get("minimum_tick_size"),
                                }
                    cached = self.token_cache.get(cid)
                    if cached:
                        result.update({
                            "maker_fee": cached.get("maker_fee"),
                            "taker_fee": cached.get("taker_fee"),
                            "min_order_size": cached.get("min_order_size"),
                            "tick_size": cached.get("tick_size"),
                        })
                        # midpoint
                        r3 = self.http.get(f"{CLOB}/midpoint", params={"token_id": cached["up"]}, timeout=2)
                        if r3.status_code == 200:
                            result["clob_mid"] = float(r3.json().get("mid", 0))
                        # top-of-book and depth for both sides
                        for side, tok in (("up", cached["up"]), ("down", cached["down"])):
                            rb = self.http.get(f"{CLOB}/book", params={"token_id": tok}, timeout=2)
                            if rb.status_code != 200:
                                continue
                            book = rb.json()
                            bids = book.get("bids", [])
                            asks = book.get("asks", [])
                            bid_px = float(bids[0]["price"]) if bids else None
                            ask_px = float(asks[0]["price"]) if asks else None
                            bid_depth = sum(float(b["size"]) * float(b["price"]) for b in bids[:10])
                            ask_depth = sum(float(a["size"]) * float(a["price"]) for a in asks[:10])
                            result[f"{side}_bid"] = bid_px
                            result[f"{side}_ask"] = ask_px
                            result[f"{side}_bid_depth"] = round(bid_depth, 4)
                            result[f"{side}_ask_depth"] = round(ask_depth, 4)
                            result[f"{side}_bid_levels"] = len(bids)
                            result[f"{side}_ask_levels"] = len(asks)
                            if bid_depth + ask_depth > 0:
                                result[f"{side}_depth_imbalance"] = round((bid_depth - ask_depth) / (bid_depth + ask_depth), 4)
                except:
                    pass
            return result
        except:
            return {}

    def _analyze_reversal(self, ticks: list, open_price: float) -> dict:
        """分析翻转概率：在不同时间点，方向是否与最终方向相反"""
        if len(ticks) < 10:
            return {}

        # 最终方向
        final_price = ticks[-1]["median"]
        final_direction = "UP" if final_price >= open_price else "DOWN"

        analysis = {}

        # 各时间点的翻转分析
        checkpoints = [
            ("last_60s", 60), ("last_50s", 50), ("last_40s", 40),
            ("last_30s", 30), ("last_20s", 20), ("last_10s", 10), ("last_5s", 5),
        ]

        for label, secs_before_end in checkpoints:
            cutoff_time = ticks[-1]["elapsed"] - secs_before_end
            # 找到那个时间点的 tick
            candidates = [t for t in ticks if t["elapsed"] <= cutoff_time]
            if not candidates:
                continue
            tick = candidates[-1]
            price_at_point = tick["median"]
            dir_at_point = "UP" if price_at_point >= open_price else "DOWN"
            mom_at_point = (price_at_point - open_price) / open_price * 100

            reversed = dir_at_point != final_direction
            analysis[label] = {
                "secs_before_end": secs_before_end,
                "price": price_at_point,
                "direction": dir_at_point,
                "momentum": round(mom_at_point, 5),
                "reversed_to_final": reversed,
                "final_direction": final_direction,
            }

        # 连续翻转事件
        reversals = []
        prev_dir = None
        for t in ticks:
            d = "UP" if t["median"] >= open_price else "DOWN"
            if prev_dir and d != prev_dir:
                reversals.append({
                    "at_elapsed": t["elapsed"],
                    "secs_left": 900 - t["elapsed"],
                    "from": prev_dir, "to": d,
                    "momentum": round((t["median"] - open_price) / open_price * 100, 5),
                })
            prev_dir = d

        # 最大动量和最大回撤
        momentums = [(t["median"] - open_price) / open_price * 100 for t in ticks]
        max_up = max(momentums) if momentums else 0
        max_down = min(momentums) if momentums else 0
        final_mom = momentums[-1] if momentums else 0

        # 波动率（标准差）
        volatility = statistics.stdev(momentums) if len(momentums) > 1 else 0

        # 尾盘 1 分钟专门分析
        last_minute_reversals = [r for r in reversals if r["secs_left"] <= 60]
        last_minute_ticks = [t for t in ticks if t["secs_left"] <= 60]
        bucket_counts = {
            "60_50": 0,
            "50_40": 0,
            "40_30": 0,
            "30_20": 0,
            "20_10": 0,
            "10_5": 0,
            "5_0": 0,
        }
        for r in last_minute_reversals:
            sl = r["secs_left"]
            if 50 < sl <= 60:
                bucket_counts["60_50"] += 1
            elif 40 < sl <= 50:
                bucket_counts["50_40"] += 1
            elif 30 < sl <= 40:
                bucket_counts["40_30"] += 1
            elif 20 < sl <= 30:
                bucket_counts["30_20"] += 1
            elif 10 < sl <= 20:
                bucket_counts["20_10"] += 1
            elif 5 < sl <= 10:
                bucket_counts["10_5"] += 1
            elif 0 <= sl <= 5:
                bucket_counts["5_0"] += 1

        # 最后一次翻转后还稳定了多久
        stable_since_secs_left = None
        last_reversal_secs_left = None
        if last_minute_reversals:
            last_reversal_secs_left = round(last_minute_reversals[-1]["secs_left"], 1)
            stable_since_secs_left = last_reversal_secs_left
        else:
            # 尾盘无翻转 = 尾盘一直稳定
            stable_since_secs_left = 60 if last_minute_ticks else None

        # final 10s 是否稳定
        final_10s_reversals = [r for r in last_minute_reversals if r["secs_left"] <= 10]

        last_minute_summary = {
            "count": len(last_minute_reversals),
            "events": last_minute_reversals,
            "bucket_counts": bucket_counts,
            "first_reversal_secs_left": round(last_minute_reversals[0]["secs_left"], 1) if last_minute_reversals else None,
            "last_reversal_secs_left": last_reversal_secs_left,
            "stable_since_secs_left": stable_since_secs_left,
            "final_10s_stable": len(final_10s_reversals) == 0,
            "final_10s_reversals": len(final_10s_reversals),
        }

        # 原因分析（用于自学习）
        causes = []
        if len(reversals) == 0:
            causes.append("单边行情，未出现方向翻转")
        else:
            if volatility > 0.05:
                causes.append("高波动导致多次方向摇摆")
            elif volatility > 0.02:
                causes.append("中等波动，存在震荡")
            else:
                causes.append("低波动，但临近结算时发生微小翻转")

            # 最后 60 秒是否发生翻转
            if last_minute_reversals:
                causes.append(f"最后60秒发生{len(last_minute_reversals)}次翻转，说明尾盘不稳定")
                if last_reversal_secs_left is not None:
                    causes.append(f"最后一次翻转发生在结算前 {last_reversal_secs_left:.1f} 秒")
                if final_10s_reversals:
                    causes.append(f"最后10秒仍发生{len(final_10s_reversals)}次翻转，尾盘极不稳定")
                else:
                    causes.append("最后10秒未再翻转，尾盘已重新稳定")

            # 最大顺/逆向动量
            if abs(max_up) > 0.10 and abs(max_down) > 0.10:
                causes.append("窗口内曾出现双向大波动（假突破概率高）")
            elif abs(final_mom) < 0.02:
                causes.append("最终收盘接近平盘，微小噪音即可改变方向")
            elif abs(final_mom) > 0.10 and len(reversals) > 0:
                causes.append("虽然最终趋势强，但中途被短时反向波动扰动")

            # 高频噪音
            if len(reversals) >= 4:
                causes.append("高频来回切换，可能是做市商噪音/流动性薄")

        # 计算各 checkpoint 的原因提示
        checkpoint_reasons = {}
        for label, cp in analysis.items():
            if cp.get("reversed_to_final"):
                reason = []
                mom = abs(cp.get("momentum", 0))
                secs = cp.get("secs_before_end", 0)
                if mom < 0.02:
                    reason.append("当时动量太小，方向不稳")
                if secs <= 10:
                    reason.append("尾盘10秒内容易被最后一跳翻转")
                elif secs <= 30:
                    reason.append("尾盘30秒仍有噪音影响")
                if volatility > 0.03:
                    reason.append("整体波动较大")
                if not reason:
                    reason.append("阶段性方向判断与最终收盘不一致")
                checkpoint_reasons[label] = "；".join(reason)
            else:
                checkpoint_reasons[label] = "该时点方向已稳定，与最终一致"

        return {
            "checkpoints": analysis,
            "checkpoint_reasons": checkpoint_reasons,
            "total_reversals": len(reversals),
            "reversal_events": reversals[-10:],  # 最近 10 次
            "last_minute": last_minute_summary,
            "max_up_momentum": round(max_up, 5),
            "max_down_momentum": round(max_down, 5),
            "final_momentum": round(final_mom, 5),
            "final_direction": final_direction,
            "final_price": final_price,
            "open_price": open_price,
            "volatility": round(volatility, 5),
            "total_ticks": len(ticks),
            "causes": causes,
        }

    def _summarize_learning_features(self, ticks: list, poly_snaps: list, open_price: float, final_direction: str) -> dict:
        """提炼用于自学习/回测优化的特征"""
        if not ticks or not open_price:
            return {}
        momentums = [t.get("momentum_pct", 0) for t in ticks]
        velocities = [t.get("velocity_pct_per_s", 0) for t in ticks if "velocity_pct_per_s" in t]
        accelerations = [t.get("acceleration_pct_per_s2", 0) for t in ticks if "acceleration_pct_per_s2" in t]
        spreads = [t.get("source_spread_pct", 0) for t in ticks]
        consensuses = [t.get("consensus_ratio", 0) for t in ticks]

        # MFE / MAE：最终方向下的最佳/最差 excursion
        signed = []
        for m in momentums:
            signed.append(m if final_direction == "UP" else -m)
        mfe = max(signed) if signed else 0
        mae = min(signed) if signed else 0

        # 尾盘稳定性得分（越高越稳）
        tail_ticks = [t for t in ticks if t.get("secs_left", 999) <= 60]
        tail_moms = [t.get("momentum_pct", 0) for t in tail_ticks]
        tail_vol = statistics.stdev(tail_moms) if len(tail_moms) > 1 else 0
        tail_cons = statistics.mean([t.get("consensus_ratio", 0) for t in tail_ticks]) if tail_ticks else 0
        tail_spread = statistics.mean([t.get("source_spread_pct", 0) for t in tail_ticks]) if tail_ticks else 0
        stability_score = max(0, 100 - tail_vol * 800 - tail_spread * 50 + tail_cons * 20)

        # Polymarket 相关特征
        poly_features = {}
        if poly_snaps:
            mis_up = [p.get("mispricing_up", 0) for p in poly_snaps if "mispricing_up" in p]
            clob_mid = [p.get("clob_mid", 0) for p in poly_snaps if p.get("clob_mid") is not None]
            # edge 持续时间：满足有优势的快照持续多久
            edge_secs = 0
            edge_runs = []
            current_run = 0
            prev_elapsed = None
            for p in poly_snaps:
                model_up = p.get("model_up")
                if model_up is None:
                    continue
                market_prob = p.get("up", 0.5) if final_direction == "UP" else p.get("down", 0.5)
                model_prob = model_up if final_direction == "UP" else (1 - model_up)
                good = model_prob - market_prob >= 0.05 and market_prob <= 0.75
                elapsed = p.get("elapsed", 0)
                step = elapsed - prev_elapsed if prev_elapsed is not None else 0
                prev_elapsed = elapsed
                if good:
                    edge_secs += max(0, step)
                    current_run += max(0, step)
                elif current_run > 0:
                    edge_runs.append(current_run)
                    current_run = 0
            if current_run > 0:
                edge_runs.append(current_run)
            poly_features = {
                "avg_mispricing_up": round(statistics.mean(mis_up), 5) if mis_up else None,
                "max_mispricing_up": round(max(mis_up), 5) if mis_up else None,
                "clob_mid_updates": len(clob_mid),
                "avg_up_depth_imbalance": round(statistics.mean([p.get("up_depth_imbalance", 0) for p in poly_snaps if p.get("up_depth_imbalance") is not None]), 4) if poly_snaps else None,
                "avg_down_depth_imbalance": round(statistics.mean([p.get("down_depth_imbalance", 0) for p in poly_snaps if p.get("down_depth_imbalance") is not None]), 4) if poly_snaps else None,
                "edge_duration_secs": round(edge_secs, 1),
                "max_edge_run_secs": round(max(edge_runs), 1) if edge_runs else 0,
            }

        # 建议标签（后续可直接拿来筛选训练样本）
        tags = []
        if mfe > 0.08:
            tags.append("strong_trend")
        if mae < -0.03:
            tags.append("deep_pullback")
        if tail_cons >= 0.9:
            tags.append("tail_consensus")
        if stability_score >= 80:
            tags.append("stable_tail")
        if stability_score < 50:
            tags.append("unstable_tail")
        if poly_features.get("max_mispricing_up") is not None and abs(poly_features["max_mispricing_up"]) > 0.10:
            tags.append("large_mispricing")

        return {
            "mfe_pct": round(mfe, 5),
            "mae_pct": round(mae, 5),
            "avg_velocity_pct_per_s": round(statistics.mean(velocities), 6) if velocities else 0,
            "max_velocity_pct_per_s": round(max(velocities), 6) if velocities else 0,
            "min_velocity_pct_per_s": round(min(velocities), 6) if velocities else 0,
            "avg_acceleration_pct_per_s2": round(statistics.mean(accelerations), 6) if accelerations else 0,
            "tail_volatility_pct": round(tail_vol, 5),
            "tail_avg_consensus": round(tail_cons, 4),
            "tail_avg_source_spread_pct": round(tail_spread, 5),
            "stability_score": round(stability_score, 2),
            "tags": tags,
            **poly_features,
        }

    def record_round(self):
        """记录一个完整的 15 分钟窗口"""
        start_unix, start_dt, end_dt = self._window_info()
        window_label = (start_dt + timedelta(hours=8)).strftime("%Y%m%d_%H%M")

        self._log(f"📦 开始记录窗口 {window_label} (结算 {(end_dt+timedelta(hours=8)).strftime('%H:%M')})")

        # 数据结构
        coin_data = {}
        for coin in COINS:
            coin_data[coin] = {
                "open_prices": {},
                "ticks": [],
                "poly_snapshots": [],
            }

        # 采集开盘价
        for coin in COINS:
            prices = self._get_all_prices(coin)
            if prices:
                coin_data[coin]["open_prices"] = prices
                coin_data[coin]["open_median"] = statistics.median(prices.values())
            time.sleep(0.1)

        # 主循环：每 2 秒采集一次
        tick_count = 0
        while self.running:
            now = datetime.now(timezone.utc)
            secs_left = (end_dt - now).total_seconds()
            elapsed = 900 - secs_left

            if secs_left < -5:  # 窗口已过
                break

            # 采集价格
            for coin in COINS:
                try:
                    prices = self._get_all_prices(coin)
                    if not prices:
                        continue
                    median = statistics.median(prices.values())

                    # 价格源特征
                    vals = list(prices.values())
                    src_spread_pct = ((max(vals) - min(vals)) / median * 100) if len(vals) > 1 else 0
                    open_median = coin_data[coin].get("open_median", median)
                    direction_votes = {
                        "up": sum(1 for p in vals if p >= open_median),
                        "down": sum(1 for p in vals if p < open_median),
                    }
                    consensus_ratio = max(direction_votes.values()) / len(vals) if vals else 0

                    tick = {
                        "elapsed": round(elapsed, 1),
                        "secs_left": round(secs_left, 1),
                        "median": median,
                        "sources": prices,
                        "n_sources": len(prices),
                        "source_spread_pct": round(src_spread_pct, 5),
                        "consensus_ratio": round(consensus_ratio, 4),
                        "direction_votes": direction_votes,
                    }

                    # 动量/速度/加速度
                    prev_ticks = coin_data[coin]["ticks"]
                    tick["momentum_pct"] = round((median - open_median) / open_median * 100, 5) if open_median else 0
                    if prev_ticks:
                        prev = prev_ticks[-1]
                        dt = max(0.1, tick["elapsed"] - prev["elapsed"])
                        tick["velocity_pct_per_s"] = round((tick["momentum_pct"] - prev.get("momentum_pct", 0)) / dt, 6)
                        if len(prev_ticks) >= 2:
                            prev_v = prev.get("velocity_pct_per_s", 0)
                            tick["acceleration_pct_per_s2"] = round((tick["velocity_pct_per_s"] - prev_v) / dt, 6)
                    coin_data[coin]["ticks"].append(tick)

                    # Polymarket 数据（每 10 秒查一次，避免太频繁）
                    if tick_count % 5 == 0:
                        poly = self._get_poly_data(coin, start_unix)
                        if poly:
                            poly["elapsed"] = round(elapsed, 1)
                            # 估算 edge / mispricing
                            if open_median:
                                mom = (median - open_median) / open_median * 100
                                model_up = min(0.995, max(0.005, 0.5 + mom * 6))
                                poly["model_up"] = round(model_up, 4)
                                poly["mispricing_up"] = round(model_up - poly.get("up", 0.5), 4)
                                poly["mispricing_down"] = round((1 - model_up) - poly.get("down", 0.5), 4)
                            coin_data[coin]["poly_snapshots"].append(poly)

                except Exception:
                    pass
                time.sleep(0.05)

            tick_count += 1

            # 心跳
            if tick_count % 30 == 0:
                total_ticks = sum(len(cd["ticks"]) for cd in coin_data.values())
                self._log(f"  💓 {elapsed:.0f}s/{secs_left:.0f}s left | {total_ticks} ticks")

            # 尾盘加密采集：最后1分钟 0.5s，最后2分钟 1s，其余 2s
            if secs_left < 60:
                time.sleep(0.5)
            elif secs_left < 120:
                time.sleep(1)
            else:
                time.sleep(2)

        # ═══ 窗口结束，分析 ═══
        self._log(f"📊 窗口 {window_label} 结束，开始分析...")

        # 等 5 秒让价格稳定
        time.sleep(5)

        # 采集结算价
        for coin in COINS:
            prices = self._get_all_prices(coin)
            if prices:
                coin_data[coin]["close_prices"] = prices
                coin_data[coin]["close_median"] = statistics.median(prices.values())
            time.sleep(0.1)

        # 分析每个币种
        round_summary = {
            "window": window_label,
            "start_unix": start_unix,
            "start_utc": start_dt.isoformat(),
            "end_utc": end_dt.isoformat(),
            "coins": {},
        }

        for coin in COINS:
            cd = coin_data[coin]
            open_p = cd.get("open_median", 0)
            close_p = cd.get("close_median", 0)
            ticks = cd["ticks"]

            if not open_p or not ticks:
                continue

            # 翻转分析
            reversal = self._analyze_reversal(ticks, open_p)

            # Polymarket 赔率变化
            poly_snaps = cd["poly_snapshots"]
            poly_start = poly_snaps[0] if poly_snaps else {}
            poly_end = poly_snaps[-1] if poly_snaps else {}

            final_dir = "UP" if close_p >= open_p else "DOWN"
            momentum = (close_p - open_p) / open_p * 100

            learning_features = self._summarize_learning_features(ticks, poly_snaps, open_p, final_dir)

            # 候选最优下注窗口：稳定 + 有 edge + 方向一致
            candidate_entries = []
            for snap in poly_snaps:
                elapsed = snap.get("elapsed", 0)
                matching_ticks = [t for t in ticks if abs(t.get("elapsed", 0) - elapsed) <= 2.5]
                if not matching_ticks:
                    continue
                t = matching_ticks[-1]
                mom = t.get("momentum_pct", 0)
                direction = "UP" if mom >= 0 else "DOWN"
                market_price = snap.get("up", 0.5) if direction == "UP" else snap.get("down", 0.5)
                model_up = snap.get("model_up")
                if model_up is None:
                    continue
                model_prob = model_up if direction == "UP" else (1 - model_up)
                edge = model_prob - market_price
                if abs(mom) >= 0.03 and t.get("consensus_ratio", 0) >= 0.75 and market_price <= 0.75 and edge >= 0.05:
                    candidate_entries.append({
                        "elapsed": elapsed,
                        "secs_left": round(900 - elapsed, 1),
                        "direction": direction,
                        "momentum_pct": round(mom, 5),
                        "market_price": market_price,
                        "model_prob": round(model_prob, 4),
                        "edge": round(edge, 4),
                        "consensus_ratio": t.get("consensus_ratio", 0),
                    })

            coin_summary = {
                "open": open_p,
                "close": close_p,
                "direction": final_dir,
                "momentum_pct": round(momentum, 5),
                "ticks_recorded": len(ticks),
                "poly_snapshots": len(poly_snaps),
                "poly_start": {"up": poly_start.get("up"), "down": poly_start.get("down")},
                "poly_end": {"up": poly_end.get("up"), "down": poly_end.get("down")},
                "poly_volume": poly_end.get("volume", 0),
                "reversal_analysis": reversal,
                "learning_features": learning_features,
                "candidate_entries": candidate_entries[:8],
                "sources_used": list(cd.get("open_prices", {}).keys()),
            }

            round_summary["coins"][coin] = coin_summary

            # 打印摘要
            rev_info = ""
            if reversal.get("checkpoints"):
                flips = sum(1 for cp in reversal["checkpoints"].values() if cp.get("reversed_to_final"))
                rev_info = f" | 翻转点={flips}/{len(reversal['checkpoints'])}"
            lm = reversal.get("last_minute", {})
            self._log(
                f"  {coin.upper():5} {final_dir:5} mom={momentum:+.4f}% "
                f"vol={reversal.get('volatility',0):.4f}% "
                f"总翻转={reversal.get('total_reversals',0)} | 尾盘60s翻转={lm.get('count',0)}{rev_info}"
            )
            if lm:
                self._log(
                    f"    尾盘1分钟: first={lm.get('first_reversal_secs_left')}s left | "
                    f"last={lm.get('last_reversal_secs_left')}s left | "
                    f"stable_since={lm.get('stable_since_secs_left')}s left | "
                    f"final10s_stable={lm.get('final_10s_stable')}"
                )
                self._log(
                    f"    分桶统计: 60-50={lm.get('bucket_counts',{}).get('60_50',0)} | "
                    f"50-40={lm.get('bucket_counts',{}).get('50_40',0)} | "
                    f"40-30={lm.get('bucket_counts',{}).get('40_30',0)} | "
                    f"30-20={lm.get('bucket_counts',{}).get('30_20',0)} | "
                    f"20-10={lm.get('bucket_counts',{}).get('20_10',0)} | "
                    f"10-5={lm.get('bucket_counts',{}).get('10_5',0)} | "
                    f"5-0={lm.get('bucket_counts',{}).get('5_0',0)}"
                )
            for label in ["last_60s", "last_50s", "last_40s", "last_30s", "last_20s", "last_10s", "last_5s"]:
                cp = reversal.get("checkpoints", {}).get(label)
                if cp:
                    mark = "🔴翻转" if cp.get("reversed_to_final") else "🟢稳定"
                    reason = reversal.get("checkpoint_reasons", {}).get(label, "")
                    self._log(f"    {label:10} {mark} mom={cp.get('momentum',0):+8.4f}% | {reason}")
            for cause in reversal.get("causes", [])[:4]:
                self._log(f"    原因: {cause}")

        # 保存完整数据
        log_file = LOG_DIR / f"round_{window_label}.json"
        with open(log_file, "w") as f:
            json.dump(round_summary, f, indent=2, default=str)

        # 保存 tick 级别数据（用于深度分析）
        tick_file = LOG_DIR / f"ticks_{window_label}.jsonl"
        with open(tick_file, "w") as f:
            for coin in COINS:
                for tick in coin_data[coin]["ticks"]:
                    tick["coin"] = coin
                    f.write(json.dumps(tick, default=str) + "\n")

        # 保存尾盘1分钟精简报告（最适合自学习）
        tail_report = {
            "window": window_label,
            "coins": {}
        }
        for coin, coin_summary in round_summary["coins"].items():
            ra = coin_summary.get("reversal_analysis", {})
            lm = ra.get("last_minute", {})
            tail_report["coins"][coin] = {
                "direction": coin_summary.get("direction"),
                "momentum_pct": coin_summary.get("momentum_pct"),
                "last_minute_reversals": lm.get("count", 0),
                "reversal_events": lm.get("events", []),
                "bucket_counts": lm.get("bucket_counts", {}),
                "first_reversal_secs_left": lm.get("first_reversal_secs_left"),
                "last_reversal_secs_left": lm.get("last_reversal_secs_left"),
                "stable_since_secs_left": lm.get("stable_since_secs_left"),
                "final_10s_stable": lm.get("final_10s_stable"),
                "final_10s_reversals": lm.get("final_10s_reversals"),
                "causes": ra.get("causes", []),
            }
        tail_file = LOG_DIR / f"tail60_{window_label}.json"
        with open(tail_file, "w") as f:
            json.dump(tail_report, f, indent=2, default=str)

        self._log(f"💾 已保存: {log_file.name} + {tick_file.name} + {tail_file.name}")

        # 累积翻转统计
        self._update_cumulative_stats(round_summary)

        return round_summary

    def _update_cumulative_stats(self, round_data):
        """累积翻转概率统计"""
        stats_file = LOG_DIR / "cumulative_reversal_stats.json"
        try:
            if stats_file.exists():
                stats = json.loads(stats_file.read_text())
            else:
                stats = {
                    "total_rounds": 0,
                    "total_coin_windows": 0,
                    "checkpoints": {},  # label -> {total, reversed, avg_momentum_when_reversed}
                    "by_coin": {},
                    "volatility_buckets": {},  # low/mid/high -> reversal_rate
                    "entry_windows": {},      # secs_left bucket -> outcome stats
                    "feature_buckets": {},    # learning features aggregate
                }
        except:
            stats = {"total_rounds": 0, "total_coin_windows": 0, "checkpoints": {}, "by_coin": {}, "volatility_buckets": {}, "entry_windows": {}, "feature_buckets": {}}

        stats["total_rounds"] += 1

        for coin, cd in round_data.get("coins", {}).items():
            stats["total_coin_windows"] += 1
            ra = cd.get("reversal_analysis", {})

            # 每个 coin 的统计
            cs = stats["by_coin"].setdefault(coin, {
                "total": 0,
                "reversals": 0,
                "last_minute_reversals": 0,
                "last_10s_unstable": 0,
                "sum_last_reversal_secs_left": 0,
            })
            cs["total"] += 1
            cs["reversals"] += ra.get("total_reversals", 0)
            lm = ra.get("last_minute", {})
            cs["last_minute_reversals"] += lm.get("count", 0)
            if not lm.get("final_10s_stable", True):
                cs["last_10s_unstable"] += 1
            if lm.get("last_reversal_secs_left") is not None:
                cs["sum_last_reversal_secs_left"] += lm.get("last_reversal_secs_left", 0)

            # Checkpoint 翻转率
            for label, cp in ra.get("checkpoints", {}).items():
                s = stats["checkpoints"].setdefault(label, {"total": 0, "reversed": 0, "mom_sum": 0})
                s["total"] += 1
                if cp.get("reversed_to_final"):
                    s["reversed"] += 1
                    s["mom_sum"] += abs(cp.get("momentum", 0))

            # 波动率分桶
            vol = ra.get("volatility", 0)
            if vol < 0.02:
                bucket = "low"
            elif vol < 0.05:
                bucket = "mid"
            else:
                bucket = "high"
            vb = stats["volatility_buckets"].setdefault(bucket, {"total": 0, "any_reversal": 0})
            vb["total"] += 1
            if ra.get("total_reversals", 0) > 0:
                vb["any_reversal"] += 1

            # 学习特征分桶
            lf = cd.get("learning_features", {})
            stability = lf.get("stability_score", 0)
            stab_bucket = "high" if stability >= 80 else "mid" if stability >= 60 else "low"
            fb = stats["feature_buckets"].setdefault(stab_bucket, {"total": 0, "avg_tail_reversals_sum": 0, "wins_if_follow_final": 0})
            fb["total"] += 1
            fb["avg_tail_reversals_sum"] += ra.get("last_minute", {}).get("count", 0)
            fb["wins_if_follow_final"] += 1  # 标签样本计数，占位可扩展

            # 候选入场窗口统计（为了找“稳的边际”）
            for ce in cd.get("candidate_entries", [])[:1]:
                sl = ce.get("secs_left", 0)
                if sl >= 180:
                    e_bucket = "180_plus"
                elif sl >= 120:
                    e_bucket = "180_120"
                elif sl >= 60:
                    e_bucket = "120_60"
                elif sl >= 30:
                    e_bucket = "60_30"
                elif sl >= 10:
                    e_bucket = "30_10"
                else:
                    e_bucket = "10_0"
                eb = stats["entry_windows"].setdefault(e_bucket, {"total": 0, "edge_sum": 0, "momentum_sum": 0})
                eb["total"] += 1
                eb["edge_sum"] += ce.get("edge", 0)
                eb["momentum_sum"] += abs(ce.get("momentum_pct", 0))

        # 计算翻转概率
        stats["reversal_probabilities"] = {}
        for label, s in stats["checkpoints"].items():
            if s["total"] > 0:
                rate = s["reversed"] / s["total"]
                avg_mom = s["mom_sum"] / s["reversed"] if s["reversed"] else 0
                stats["reversal_probabilities"][label] = {
                    "rate": round(rate, 4),
                    "pct": f"{rate*100:.1f}%",
                    "sample_size": s["total"],
                    "avg_momentum_at_reversal": round(avg_mom, 5),
                }

        # 派生每币种稳定性统计
        stats["coin_stability"] = {}
        for coin, cs in stats["by_coin"].items():
            total = cs.get("total", 0) or 1
            stats["coin_stability"][coin] = {
                "avg_reversals_per_round": round(cs.get("reversals", 0) / total, 3),
                "avg_last_minute_reversals": round(cs.get("last_minute_reversals", 0) / total, 3),
                "last_10s_unstable_rate": round(cs.get("last_10s_unstable", 0) / total, 4),
                "avg_last_reversal_secs_left": round(cs.get("sum_last_reversal_secs_left", 0) / total, 2) if cs.get("sum_last_reversal_secs_left", 0) else None,
            }

        stats["entry_window_summary"] = {}
        for bucket, eb in stats.get("entry_windows", {}).items():
            total = eb.get("total", 0) or 1
            stats["entry_window_summary"][bucket] = {
                "count": eb.get("total", 0),
                "avg_edge": round(eb.get("edge_sum", 0) / total, 5),
                "avg_abs_momentum": round(eb.get("momentum_sum", 0) / total, 5),
            }

        stats["feature_bucket_summary"] = {}
        for bucket, fb in stats.get("feature_buckets", {}).items():
            total = fb.get("total", 0) or 1
            stats["feature_bucket_summary"][bucket] = {
                "count": fb.get("total", 0),
                "avg_tail_reversals": round(fb.get("avg_tail_reversals_sum", 0) / total, 4),
            }

        stats["last_updated"] = datetime.now(timezone.utc).isoformat()

        with open(stats_file, "w") as f:
            json.dump(stats, f, indent=2)

        # 打印累积统计
        self._log(f"📈 累积翻转概率 ({stats['total_coin_windows']} 样本):")
        for label in ["last_60s", "last_50s", "last_40s", "last_30s", "last_20s", "last_10s", "last_5s"]:
            rp = stats.get("reversal_probabilities", {}).get(label, {})
            if rp:
                self._log(f"  {label:10} → 翻转率 {rp['pct']:>6} (n={rp['sample_size']})")
        if stats.get("coin_stability"):
            self._log("📌 每币种尾盘稳定性:")
            for coin, s in sorted(stats["coin_stability"].items()):
                self._log(
                    f"  {coin.upper():5} 尾盘翻转均值={s['avg_last_minute_reversals']:.2f} | "
                    f"最后10s不稳率={s['last_10s_unstable_rate']*100:.1f}% | "
                    f"平均最后翻转点={s['avg_last_reversal_secs_left']}s"
                )
        if stats.get("entry_window_summary"):
            self._log("🎯 候选入场窗口统计:")
            for bucket, s in stats["entry_window_summary"].items():
                self._log(f"  {bucket:8} count={s['count']} avg_edge={s['avg_edge']:+.4f} avg_mom={s['avg_abs_momentum']:.4f}%")

    def run(self):
        print("=" * 65, flush=True)
        print("📝 Polymarket 15min Round Logger (7 coins)", flush=True)
        print("=" * 65, flush=True)
        print(f"  币种: {', '.join(c.upper() for c in COINS)}", flush=True)
        print(f"  价格源: {', '.join(PRICE_SOURCES.keys())}", flush=True)
        print(f"  记录: {LOG_DIR}", flush=True)
        print(f"  PID: {os.getpid()}", flush=True)
        print("=" * 65, flush=True)

        while self.running:
            try:
                # 等待下一个窗口开始
                now = datetime.now(timezone.utc)
                ws = (now.minute // 15) * 15
                start = now.replace(minute=ws, second=0, microsecond=0)
                next_start = start + timedelta(minutes=15)
                wait = (next_start - now).total_seconds()

                if wait > 5:
                    self._log(f"⏳ 等待下一窗口: {wait:.0f}s ({(next_start+timedelta(hours=8)).strftime('%H:%M')} GMT+8)")
                    # 分段等待，以便响应 SIGTERM
                    for _ in range(int(wait)):
                        if not self.running:
                            break
                        time.sleep(1)
                    if not self.running:
                        break
                    time.sleep(max(0, wait - int(wait)))

                # 记录这个窗口
                self.record_round()

            except Exception as e:
                self._log(f"❌ 异常: {e}")
                time.sleep(10)

        self._log("⏹ Logger 停止")


if __name__ == "__main__":
    logger = RoundLogger()
    logger.run()
