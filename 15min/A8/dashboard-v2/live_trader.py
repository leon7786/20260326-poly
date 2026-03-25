#!/usr/bin/env python3
"""
Polymarket Live Trader

目标：
- 复用 paper_trader_v3 的 WSS 行情、窗口与信号逻辑
- 接入 Polymarket CLOB Level 2 auth（proxy/funder 模式）
- 支持 dry-run / real live 两种模式
- 使用独立日志 / 状态文件，不污染 paper trader
- 只管理 bot 自己创建并记录的订单，不碰用户历史手动单

⚠️ 安全原则：
- 只有真实成交（filled / partial fill）才进入持仓与结算
- 未成交挂单只算 pending order，不算持仓
- 只 cancel bot-managed order_id
- 重启后先 reconcile bot 自己的订单状态，再继续运行
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone, timedelta

from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    BalanceAllowanceParams,
    AssetType,
    OrderArgs,
    PartialCreateOrderOptions,
    OrderType,
    OpenOrderParams,
)
from py_order_utils.model import POLY_PROXY

import paper_trader_v3 as pt3

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"
DATA_DIR = BASE_DIR / "data"
ENV_FILE = BASE_DIR / ".env.live"
POLYGON_RPC = "https://polygon.drpc.org"
CLOB_HOST = "https://clob.polymarket.com"

# Live 独立文件，避免污染 paper trader
LIVE_TRADE_LOG = OUTPUT_DIR / "live_trades.jsonl"
LIVE_EVENT_LOG = OUTPUT_DIR / "live_events.jsonl"
LIVE_STATE_FILE = DATA_DIR / "live_state.json"
LIVE_PID_FILE = OUTPUT_DIR / "live_trader.pid"

# monkey-patch base module globals，当前进程内让继承方法使用 live 独立路径
pt3.TRADE_LOG = LIVE_TRADE_LOG
pt3.STATE_FILE = LIVE_STATE_FILE
pt3.PID_FILE = LIVE_PID_FILE

PaperTraderV3 = pt3.PaperTraderV3
Config = pt3.Config
estimate_win_probability = pt3.estimate_win_probability

TERMINAL_ORDER_STATUSES = {"filled", "cancelled", "rejected", "expired", "settled"}
OPEN_ORDER_STATUSES = {"submitted", "open", "partially_filled", "cancel_requested"}


def now_ts() -> float:
    return time.time()


def safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def extract_order_id(resp) -> str | None:
    """尽量从 post_order 响应里提取 order id。"""
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except Exception:
            return None
    if isinstance(resp, dict):
        for key in ("orderID", "orderId", "order_id", "id"):
            val = resp.get(key)
            if isinstance(val, str) and val.startswith("0x"):
                return val
        for value in resp.values():
            oid = extract_order_id(value)
            if oid:
                return oid
    if isinstance(resp, list):
        for item in resp:
            oid = extract_order_id(item)
            if oid:
                return oid
    return None


class LiveTrader(PaperTraderV3):
    def _load_state(self):
        """覆盖父类：加载 live 独立状态。"""
        self.total_pnl = 0.0
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.today = ""
        self.total_wins = 0
        self.total_count = 0
        self.total_skips = 0

        self.windows = {}
        self.bot_orders = {}
        self.live_positions = {}
        self.reconcile_meta = {"last_sync_ts": 0}

        if LIVE_STATE_FILE.exists():
            try:
                s = json.loads(LIVE_STATE_FILE.read_text())
                self.total_pnl = s.get("total_pnl", 0.0)
                self.daily_pnl = s.get("daily_pnl", 0.0)
                self.daily_trades = s.get("daily_trades", 0)
                self.today = s.get("today", "")
                self.total_wins = s.get("total_wins", 0)
                self.total_count = s.get("total_count", 0)
                self.total_skips = s.get("total_skips", 0)
                self.windows = s.get("windows", {}) or {}
                self.bot_orders = s.get("bot_orders", {}) or {}
                self.live_positions = s.get("live_positions", {}) or {}
                self.reconcile_meta = s.get("reconcile_meta", {"last_sync_ts": 0}) or {"last_sync_ts": 0}
            except Exception:
                pass

    def _save_state(self):
        wr = (self.total_wins / self.total_count * 100) if self.total_count else 0
        LIVE_STATE_FILE.write_text(json.dumps({
            "total_pnl": round(self.total_pnl, 6),
            "daily_pnl": round(self.daily_pnl, 6),
            "daily_trades": self.daily_trades,
            "today": self.today,
            "total_wins": self.total_wins,
            "total_count": self.total_count,
            "total_skips": self.total_skips,
            "win_rate": round(wr, 2),
            "updated": datetime.now(timezone.utc).isoformat(),
            "sources": self.prices.source_status(),
            "pid": os.getpid(),
            "windows": self.windows,
            "bot_orders": self.bot_orders,
            "live_positions": self.live_positions,
            "reconcile_meta": self.reconcile_meta,
        }, indent=2))

    def __init__(self, config=None):
        self.env = load_env(ENV_FILE)
        cfg = config or Config(max_buy_price=float(self.env.get("MAX_BUY_PRICE", "0.30")))
        super().__init__(config=cfg)

        self.live_enabled = self.env.get("LIVE_ENABLED", "false").lower() == "true"
        self.dry_run = self.env.get("DRY_RUN", "true").lower() == "true"
        self.max_order_usdc = float(self.env.get("MAX_ORDER_USDC", "1"))
        self.max_daily_loss_usdc = float(self.env.get("MAX_DAILY_LOSS_USDC", "2"))
        self.max_concurrent_positions = int(self.env.get("MAX_CONCURRENT_POSITIONS", "1"))
        self.allowed_coins = {c.strip().lower() for c in self.env.get("ALLOWED_COINS", "BTC,ETH,SOL").split(",") if c.strip()}
        self.live_budget_usdc = float(self.env.get("LIVE_BUDGET_USDC", "10"))
        self.order_mode = self.env.get("ORDER_MODE", "maker").lower()
        self.order_timeout_seconds = int(self.env.get("ORDER_TIMEOUT_SECONDS", "20"))
        self.sync_interval_seconds = float(self.env.get("SYNC_INTERVAL_SECONDS", "5"))

        self._init_clob()
        self._reconcile_startup()

    def _init_clob(self):
        self.clob = ClobClient(
            CLOB_HOST,
            chain_id=int(self.env.get("POLYMARKET_CHAIN_ID", "137")),
            key=self.env["POLYMARKET_PRIVATE_KEY"],
            signature_type=POLY_PROXY,
            funder=self.env.get("POLYMARKET_FUNDER_ADDRESS") or self.env.get("POLYMARKET_WALLET_ADDRESS"),
        )
        self.clob.set_api_creds(self.clob.create_or_derive_api_creds())

    def _record_event(self, payload: dict):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        with open(LIVE_EVENT_LOG, "a") as f:
            f.write(json.dumps(payload) + "\n")

    def _get_native_pol(self) -> float:
        try:
            funder = self.env.get("POLYMARKET_FUNDER_ADDRESS") or self.env.get("POLYMARKET_WALLET_ADDRESS")
            w3 = Web3(Web3.HTTPProvider(POLYGON_RPC, request_kwargs={"timeout": 15}))
            if not w3.is_connected() or not funder:
                return 0.0
            return float(w3.from_wei(w3.eth.get_balance(Web3.to_checksum_address(funder)), "ether"))
        except Exception:
            return 0.0

    def _get_usdc_balance(self) -> float:
        bal = self.clob.get_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=POLY_PROXY,
        ))
        return float(bal.get("balance", "0")) / 1_000_000

    def _managed_pending_orders_count(self) -> int:
        return sum(1 for o in self.bot_orders.values() if o.get("status") in OPEN_ORDER_STATUSES)

    def _active_live_positions_count(self) -> int:
        return sum(1 for p in self.live_positions.values() if not p.get("settled"))

    def _reserved_budget(self) -> float:
        pending = sum(float(o.get("order_usdc", 0)) for o in self.bot_orders.values() if o.get("status") in OPEN_ORDER_STATUSES)
        open_pos = sum(float(p.get("cost_usdc", 0)) for p in self.live_positions.values() if not p.get("settled"))
        return round(pending + open_pos, 6)

    def _window_has_active_work(self, window_key: str) -> bool:
        w = self.windows.get(window_key, {})
        if w.get("pending_order") or w.get("position_active"):
            return True
        pos = self.live_positions.get(window_key)
        if pos and not pos.get("settled"):
            return True
        return False

    def _live_guard_ok(self, coin: str, buy_price: float) -> tuple[bool, str]:
        if coin.lower() not in self.allowed_coins:
            return False, f"coin {coin} not allowed"
        if self._managed_pending_orders_count() + self._active_live_positions_count() >= self.max_concurrent_positions:
            return False, f"max concurrent slots reached: {self.max_concurrent_positions}"
        if self.daily_pnl <= -abs(self.max_daily_loss_usdc):
            return False, f"daily loss limit hit: {self.daily_pnl}"
        max_buy_price = float(self.env.get("MAX_BUY_PRICE", "0.30"))
        if (buy_price - max_buy_price) > 1e-9:
            return False, f"buy price too high: {buy_price}"

        order_usdc = min(self.max_order_usdc, self.live_budget_usdc)
        if self._reserved_budget() + order_usdc > self.live_budget_usdc + 1e-9:
            return False, f"live budget exceeded: reserved={self._reserved_budget():.2f}, cap={self.live_budget_usdc:.2f}"

        usdc_balance = self._get_usdc_balance()
        if usdc_balance < order_usdc:
            return False, f"insufficient USDC: {usdc_balance:.4f}"

        native_pol = self._get_native_pol()
        if native_pol <= 0:
            return True, "warning: native POL is 0, continuing because CLOB auth+allowance are ready"
        return True, "ok"

    def _normalize_order_status(self, raw_status: str, matched_size: float, original_size: float, in_open_list: bool = False) -> str:
        raw = (raw_status or "").upper()
        if in_open_list:
            if matched_size <= 0:
                return "open"
            if original_size > 0 and matched_size + 1e-9 < original_size:
                return "partially_filled"
            return "filled"
        if raw in {"LIVE", "OPEN", "ACTIVE"}:
            if matched_size <= 0:
                return "open"
            if original_size > 0 and matched_size + 1e-9 < original_size:
                return "partially_filled"
            return "filled"
        if raw in {"MATCHED", "FILLED", "EXECUTED"}:
            return "filled"
        if raw in {"CANCELED", "CANCELLED"}:
            if matched_size <= 0:
                return "cancelled"
            if original_size > 0 and matched_size + 1e-9 < original_size:
                return "partially_filled"
            return "filled"
        if raw in {"REJECTED", "FAILED", "ERROR"}:
            return "rejected"
        if raw in {"EXPIRED"}:
            return "expired"
        if matched_size > 0:
            if original_size > 0 and matched_size + 1e-9 < original_size:
                return "partially_filled"
            return "filled"
        return "submitted"

    def _register_bot_order(self, *, window_key: str, coin: str, market_id: str | None, token_id: str, direction: str,
                             requested_price: float, requested_size: float, order_usdc: float, price_source: str,
                             submit_response: dict, order_id: str | None):
        ts = now_ts()
        order = {
            "managed": True,
            "window_key": window_key,
            "coin": coin,
            "market_id": market_id,
            "token_id": token_id,
            "direction": direction,
            "requested_price": requested_price,
            "requested_size": requested_size,
            "original_size": requested_size,
            "matched_size": 0.0,
            "remaining_size": requested_size,
            "avg_entry_price": requested_price,
            "cost_basis_source": "order_price_estimate",
            "order_usdc": order_usdc,
            "price_source": price_source,
            "status": "submitted" if order_id else "rejected",
            "raw_status": None,
            "created_at": ts,
            "timeout_at": ts + self.order_timeout_seconds,
            "last_sync_at": ts,
            "last_seen_open": False,
            "submit_response": submit_response,
            "cancel_response": None,
        }
        if order_id:
            order["order_id"] = order_id
            self.bot_orders[order_id] = order
        return order

    def _release_window_after_no_fill(self, order: dict):
        window_key = order.get("window_key")
        w = self.windows.get(window_key)
        if not w:
            return
        w["pending_order"] = False
        w["pending_order_id"] = None
        w["pending_order_status"] = order.get("status")
        w["position_active"] = False
        w["live_action"] = order.get("status")

    def _upsert_position_from_order(self, order: dict):
        matched_size = safe_float(order.get("matched_size"), 0)
        if matched_size <= 0:
            return None

        window_key = order["window_key"]
        w = self.windows.get(window_key, {})
        pos = self.live_positions.get(window_key)
        avg_entry = safe_float(order.get("avg_entry_price"), safe_float(order.get("requested_price"), 0))
        cost_usdc = matched_size * avg_entry
        if not pos:
            pos = {
                "window_key": window_key,
                "coin": order.get("coin"),
                "direction": order.get("direction"),
                "start_unix": w.get("start_unix"),
                "open_price": w.get("open_price"),
                "entry_price": w.get("entry_price"),
                "avg_entry_price": avg_entry,
                "filled_size": matched_size,
                "cost_usdc": cost_usdc,
                "price_source": order.get("price_source"),
                "cost_basis_source": order.get("cost_basis_source", "order_price_estimate"),
                "order_ids": [order.get("order_id")],
                "settled": False,
                "created_at": now_ts(),
            }
            self.live_positions[window_key] = pos
        else:
            pos["filled_size"] = matched_size
            pos["avg_entry_price"] = avg_entry
            pos["cost_usdc"] = cost_usdc
            if order.get("order_id") and order.get("order_id") not in pos.get("order_ids", []):
                pos.setdefault("order_ids", []).append(order.get("order_id"))

        w["traded"] = True
        w["live"] = True
        w["direction"] = order.get("direction")
        w["buy_price"] = avg_entry
        w["pending_order"] = False
        w["position_active"] = True
        w["pending_order_id"] = None
        return pos

    def _update_bot_order_from_snapshot(self, order: dict, snapshot: dict, in_open_list: bool) -> bool:
        changed = False
        prev_status = order.get("status")
        prev_matched = safe_float(order.get("matched_size"), 0)
        prev_cost_source = order.get("cost_basis_source")

        matched_size = safe_float(snapshot.get("size_matched"), prev_matched)
        original_size = safe_float(snapshot.get("original_size"), order.get("original_size", order.get("requested_size", 0)))
        associate_trades = snapshot.get("associate_trades") or []
        avg_entry = safe_float(order.get("requested_price"), 0)
        cost_source = order.get("cost_basis_source", "order_price_estimate")
        if isinstance(associate_trades, list) and associate_trades:
            prices = []
            weights = []
            for t in associate_trades:
                if not isinstance(t, dict):
                    continue
                p = safe_float(t.get("price"), 0)
                s = safe_float(t.get("size"), 0)
                if p > 0 and s > 0:
                    prices.append(p)
                    weights.append(s)
            if prices and weights and sum(weights) > 0:
                avg_entry = sum(p * s for p, s in zip(prices, weights)) / sum(weights)
                cost_source = "trade_history"

        status = self._normalize_order_status(snapshot.get("status"), matched_size, original_size, in_open_list)

        order["raw_status"] = snapshot.get("status")
        order["matched_size"] = matched_size
        order["original_size"] = original_size
        order["remaining_size"] = max(original_size - matched_size, 0.0)
        order["last_sync_at"] = now_ts()
        order["last_seen_open"] = in_open_list
        order["avg_entry_price"] = avg_entry
        order["cost_basis_source"] = cost_source
        order["status"] = status

        if prev_status != status or abs(prev_matched - matched_size) > 1e-9 or prev_cost_source != cost_source:
            changed = True

        if matched_size > prev_matched:
            self._record_event({
                "action": "fill_update",
                "order_id": order.get("order_id"),
                "coin": order.get("coin"),
                "status": status,
                "matched_size": matched_size,
                "avg_entry_price": avg_entry,
                "cost_basis_source": cost_source,
            })
            self._log(
                f"✅ LIVE fill update {order.get('coin', '').upper()} | order={order.get('order_id')} "
                f"| matched={matched_size:.4f}/{original_size:.4f} | status={status}"
            )

        if matched_size > 0:
            self._upsert_position_from_order(order)
        elif status in {"cancelled", "rejected", "expired"}:
            self._release_window_after_no_fill(order)

        if status == "filled":
            w = self.windows.get(order.get("window_key"), {})
            w["position_active"] = True
            w["pending_order"] = False
        return changed

    def _cancel_bot_order(self, order_id: str, reason: str) -> bool:
        order = self.bot_orders.get(order_id)
        if not order or order.get("status") in TERMINAL_ORDER_STATUSES:
            return False
        try:
            resp = self.clob.cancel(order_id)
            order["cancel_response"] = resp
            order["status"] = "cancel_requested"
            order["cancel_requested_at"] = now_ts()
            self._record_event({
                "action": "cancel_requested",
                "reason": reason,
                "order_id": order_id,
                "coin": order.get("coin"),
            })
            self._log(f"🛑 LIVE cancel requested | {order.get('coin', '').upper()} | order={order_id} | reason={reason}")
            return True
        except Exception as e:
            self._record_event({
                "action": "cancel_error",
                "reason": reason,
                "order_id": order_id,
                "error": str(e),
            })
            self._log(f"⚠️ LIVE cancel 失败 | order={order_id} | {e}")
            return False

    def _cancel_timed_out_orders(self, now_value: float | None = None) -> int:
        now_value = now_value or now_ts()
        cancelled = 0
        for order_id, order in list(self.bot_orders.items()):
            if order.get("status") not in OPEN_ORDER_STATUSES:
                continue
            if now_value >= safe_float(order.get("timeout_at"), 0):
                if self._cancel_bot_order(order_id, reason="timeout"):
                    cancelled += 1
        return cancelled

    def _cancel_expired_window_orders(self, now_value: float | None = None) -> int:
        now_value = now_value or now_ts()
        cancelled = 0
        for order_id, order in list(self.bot_orders.items()):
            if order.get("status") not in OPEN_ORDER_STATUSES:
                continue
            w = self.windows.get(order.get("window_key"), {})
            end_ts = safe_float(w.get("start_unix"), 0) + 900
            if end_ts > 0 and now_value >= end_ts - 2:
                if self._cancel_bot_order(order_id, reason="window_expiring"):
                    cancelled += 1
        return cancelled

    def _sync_bot_orders(self, force: bool = False):
        now_value = now_ts()
        last_sync = safe_float(self.reconcile_meta.get("last_sync_ts"), 0)
        if not force and (now_value - last_sync) < self.sync_interval_seconds:
            return
        self.reconcile_meta["last_sync_ts"] = now_value

        if not self.bot_orders:
            return

        try:
            open_orders = self.clob.get_orders(OpenOrderParams())
            open_map = {o.get("id"): o for o in open_orders if isinstance(o, dict) and o.get("id")}
        except Exception as e:
            self._log(f"⚠️ LIVE 同步 open orders 失败: {e}")
            return

        changed = False
        for order_id, order in list(self.bot_orders.items()):
            if order.get("status") in TERMINAL_ORDER_STATUSES:
                continue

            snapshot = open_map.get(order_id)
            in_open_list = snapshot is not None
            if snapshot is None:
                try:
                    snapshot = self.clob.get_order(order_id)
                except Exception:
                    snapshot = None

            if snapshot is not None:
                if self._update_bot_order_from_snapshot(order, snapshot, in_open_list):
                    changed = True

        if self._cancel_timed_out_orders(now_value=now_value) > 0:
            changed = True
        if self._cancel_expired_window_orders(now_value=now_value) > 0:
            changed = True

        if changed:
            self._save_state()

    def _reconcile_startup(self):
        """重启后只 reconcile bot 自己的订单，不碰用户历史手动单。"""
        if not isinstance(self.bot_orders, dict):
            self.bot_orders = {}
        if not isinstance(self.live_positions, dict):
            self.live_positions = {}
        if not isinstance(self.windows, dict):
            self.windows = {}
        if self.bot_orders:
            self._log(f"🔄 LIVE 恢复 bot-managed orders: {len(self.bot_orders)}")
            self._sync_bot_orders(force=True)
            self._save_state()

    def _build_order_payload(self, *, window_key: str, coin: str, market: dict, direction: str, buy_price: float, price_source: str):
        token = market.get("_up_token") if direction == "UP" else market.get("_down_token")
        if not token:
            return False, "missing token", None

        ok, reason = self._live_guard_ok(coin, buy_price)
        if not ok:
            self._record_event({
                "action": "skip",
                "coin": coin,
                "direction": direction,
                "buy_price": buy_price,
                "token_id": token,
                "reason": reason,
            })
            return False, reason, None

        book = self.clob.get_order_book(token)
        tick_size = book.tick_size or "0.01"
        neg_risk = bool(book.neg_risk)
        order_usdc = min(self.max_order_usdc, self.live_budget_usdc - self._reserved_budget())
        size = max(order_usdc / max(buy_price, 0.01), 1.0)
        size = round(size, 2)

        signed = self.clob.create_order(
            OrderArgs(
                token_id=token,
                price=buy_price,
                size=size,
                side="BUY",
            ),
            PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=neg_risk,
            )
        )

        payload = {
            "window_key": window_key,
            "coin": coin,
            "market_id": market.get("conditionId"),
            "direction": direction,
            "buy_price": buy_price,
            "price_source": price_source,
            "token_id": token,
            "tick_size": tick_size,
            "neg_risk": neg_risk,
            "order_usdc": order_usdc,
            "size": size,
            "guard_reason": reason,
            "order_mode": self.order_mode,
            "signed": signed,
        }
        return True, reason, payload

    def _submit_live_order(self, *, window_key: str, coin: str, market: dict, direction: str, buy_price: float, price_source: str):
        ok, reason, payload = self._build_order_payload(
            window_key=window_key,
            coin=coin,
            market=market,
            direction=direction,
            buy_price=buy_price,
            price_source=price_source,
        )
        if not ok:
            return False, reason, None

        payload_to_log = {k: v for k, v in payload.items() if k != "signed"}

        if self.dry_run or not self.live_enabled:
            payload_to_log["action"] = "dry_run_signed"
            self._record_event(payload_to_log)
            return True, "dry_run_signed", payload_to_log

        try:
            resp = self.clob.post_order(payload["signed"], orderType=OrderType.GTC)
            order_id = extract_order_id(resp)
            payload_to_log["action"] = "posted"
            payload_to_log["response"] = resp
            payload_to_log["order_id"] = order_id

            order = self._register_bot_order(
                window_key=window_key,
                coin=coin,
                market_id=payload.get("market_id"),
                token_id=payload["token_id"],
                direction=direction,
                requested_price=buy_price,
                requested_size=payload["size"],
                order_usdc=payload["order_usdc"],
                price_source=price_source,
                submit_response=resp,
                order_id=order_id,
            )

            self._record_event(payload_to_log)
            self._save_state()
            if not order_id:
                return False, "missing order id in response", payload_to_log
            return True, "submitted", payload_to_log
        except Exception as e:
            payload_to_log["action"] = "submit_error"
            payload_to_log["error"] = str(e)
            self._record_event(payload_to_log)
            return False, f"submit error: {e}", payload_to_log

    def _settle_all_due(self):
        now_value = now_ts()
        to_settle = []
        for window_key, pos in list(self.live_positions.items()):
            if not isinstance(pos, dict):
                continue
            if pos.get("settled"):
                continue
            start_unix = safe_float(pos.get("start_unix"), 0)
            if start_unix and now_value >= start_unix + 900 + 5:
                to_settle.append((window_key, pos))

        for window_key, pos in to_settle:
            coin = pos.get("coin")
            try:
                close_price = self.prices.get_median_price(coin)
                if close_price is None:
                    continue
                self._do_live_settle(window_key, pos, close_price)
            except Exception as e:
                self._log(f"⚠️ LIVE 结算异常 {window_key}: {e}")

    def _do_live_settle(self, window_key: str, pos: dict, close_price: float):
        pos["settled"] = True
        pos["close_price"] = close_price
        direction = pos["direction"]
        open_p = safe_float(pos.get("open_price"), 0)
        avg_entry = safe_float(pos.get("avg_entry_price"), 0)
        filled_size = safe_float(pos.get("filled_size"), 0)

        won = (close_price > open_p) if direction == "UP" else (close_price < open_p)
        pnl_per_share = (1.0 - avg_entry) if won else -avg_entry
        pnl = pnl_per_share * filled_size

        pos["won"] = won
        pos["pnl"] = round(pnl, 6)
        pos["settled_at"] = datetime.now(timezone.utc).isoformat()

        self.total_pnl += pnl
        self.daily_pnl += pnl
        self.daily_trades += 1
        self.total_count += 1
        if won:
            self.total_wins += 1

        w = self.windows.get(window_key, {})
        w["settled"] = True
        w["position_active"] = False
        w["close_price"] = close_price
        w["won"] = won
        w["pnl"] = round(pnl, 6)

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "window_key": window_key,
            "coin": pos.get("coin"),
            "direction": direction,
            "open": open_p,
            "entry": pos.get("entry_price"),
            "close": close_price,
            "matched_size": filled_size,
            "avg_entry_price": avg_entry,
            "cost_usdc": round(safe_float(pos.get("cost_usdc"), 0), 6),
            "cost_basis_source": pos.get("cost_basis_source", "order_price_estimate"),
            "price_source": pos.get("price_source", "?"),
            "order_ids": pos.get("order_ids", []),
            "won": won,
            "pnl": round(pnl, 6),
            "total_pnl": round(self.total_pnl, 6),
        }
        with open(LIVE_TRADE_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")

        self._record_event({
            "action": "settled",
            "window_key": window_key,
            "coin": pos.get("coin"),
            "direction": direction,
            "matched_size": filled_size,
            "avg_entry_price": avg_entry,
            "close_price": close_price,
            "won": won,
            "pnl": round(pnl, 6),
        })
        self._save_state()

        icon = "✅" if won else "❌"
        wr = (self.total_wins / self.total_count * 100) if self.total_count else 0
        self._log(
            f"{icon} LIVE {pos.get('coin', '').upper()} {direction} | size={filled_size:.4f} | ${pnl:+.4f} "
            f"| 累计${self.total_pnl:+.4f} WR={wr:.0f}% ({self.total_count}笔)"
        )

    def _cleanup_old_windows(self):
        cutoff = now_ts() - 3600
        to_del = []
        for key, v in self.windows.items():
            if not isinstance(v, dict):
                continue
            if safe_float(v.get("start_unix"), 0) >= cutoff:
                continue
            if v.get("pending_order") or v.get("position_active") or not v.get("settled", False):
                continue
            to_del.append(key)
        for key in to_del:
            del self.windows[key]

    def _check_and_trade(self, coin):
        start_unix, _, end_dt = self._window_ts()
        now_dt = datetime.now(timezone.utc)
        secs_left = (end_dt - now_dt).total_seconds()
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
                "attempted": False,
                "pending_order": False,
                "pending_order_id": None,
                "position_active": False,
            }
            end_local = (end_dt + timedelta(hours=8)).strftime("%H:%M")
            self._log(f"📦 {coin.upper()} LIVE 窗口 → {end_local} | 开盘 ${price:,.1f}")

        w = self.windows[window_key]
        if w.get("settled"):
            return
        if w.get("attempted") or self._window_has_active_work(window_key):
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

        if self.cfg.strict_real_odds and price_source == "none":
            sk = f"{window_key}-{coin}-nobook"
            if sk not in self._logged_skips:
                self._logged_skips.add(sk)
                self._log(f"⏭ LIVE {coin.upper()} {direction} | prob={win_prob:.0%} mom={momentum:+.3f}% | 无盘口")
            self.total_skips += 1
            return
        if buy_price is not None and (buy_price - self.cfg.max_buy_price) > 1e-9:
            sk = f"{window_key}-{coin}-expensive"
            if sk not in self._logged_skips:
                self._logged_skips.add(sk)
                self._log(f"⏭ LIVE {coin.upper()} {direction} | ask=${buy_price:.2f}({price_source}) > ${self.cfg.max_buy_price:.2f} | book={book_info.get('bid_levels', 0)}b/{book_info.get('ask_levels', 0)}a")
            self.total_skips += 1
            return
        if buy_price is None:
            return

        w["entry_price"] = price
        w["direction"] = direction
        w["momentum"] = round(momentum, 4)
        w["win_prob"] = round(win_prob, 4)
        w["consensus"] = consensus
        w["rsi"] = round(rsi, 1)
        w["trend"] = round(trend, 4)
        w["secs_left"] = round(secs_left, 0)
        w["num_sources"] = consensus["total"]
        w["price_source"] = price_source

        ok, reason, payload = self._submit_live_order(
            window_key=window_key,
            coin=coin,
            market=market or {},
            direction=direction,
            buy_price=buy_price,
            price_source=price_source,
        )

        w["attempted"] = True
        if ok and payload and payload.get("order_id"):
            w["pending_order"] = True
            w["pending_order_id"] = payload.get("order_id")
            w["pending_order_status"] = "submitted"
        else:
            w["pending_order"] = False
            w["pending_order_id"] = None
            w["pending_order_status"] = payload.get("action") if payload else reason

        bl = book_info.get("bid_levels", 0)
        al = book_info.get("ask_levels", 0)
        icon = "🚀" if self.live_enabled and not self.dry_run else "🧪"
        if ok:
            self._log(
                f"{icon} LIVE signal {coin.upper()} {direction} | prob={win_prob:.0%} mom={momentum:+.3f}% "
                f"| ${buy_price:.2f}({price_source},{bl}b/{al}a) | {consensus['agree']}/{consensus['total']}源 "
                f"| RSI={rsi:.0f} | {secs_left:.0f}s | {reason} | action={payload.get('action') if payload else '?'}"
            )
        else:
            self._log(f"⏭ LIVE {coin.upper()} {direction} | blocked: {reason}")

        self._save_state()

    def run(self):
        cfg = self.cfg
        LIVE_PID_FILE.write_text(str(os.getpid()))

        def _sigterm(sig, frame):
            self._log("收到 SIGTERM，保存 live 状态...")
            self._save_state()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _sigterm)

        print("=" * 72, flush=True)
        print("🚀 Polymarket Live Trader (WSS + CLOB Level2)", flush=True)
        print("=" * 72, flush=True)
        print(f"  PID: {os.getpid()}", flush=True)
        print(f"  Live Enabled: {self.live_enabled}", flush=True)
        print(f"  Dry Run: {self.dry_run}", flush=True)
        print(f"  币种白名单: {', '.join(sorted(c.upper() for c in self.allowed_coins))}", flush=True)
        print(f"  单笔上限: ${self.max_order_usdc:.2f} | 总预算: ${self.live_budget_usdc:.2f}", flush=True)
        print(f"  最大并发持仓: {self.max_concurrent_positions}", flush=True)
        print(f"  最大买入价: ${self.cfg.max_buy_price:.2f} | 日亏损上限: ${self.max_daily_loss_usdc:.2f}", flush=True)
        print(f"  Order Timeout: {self.order_timeout_seconds}s | Sync: {self.sync_interval_seconds}s", flush=True)
        print(f"  Live Event Log: {LIVE_EVENT_LOG}", flush=True)
        print(f"  Live Trade Log: {LIVE_TRADE_LOG}", flush=True)
        print("=" * 72, flush=True)

        self._log("等待 WSS 连接...")
        for _ in range(100):
            ready = sum(1 for c in ["btc", "eth", "sol"] if self.prices.get_prices(c))
            if ready >= 2:
                break
            time.sleep(0.1)

        self._log(f"源状态: {self.prices.source_status()}")
        self._log(f"Live 余额: USDC=${self._get_usdc_balance():.4f} | POL={self._get_native_pol():.6f}")
        self._log("开始监控 live 信号...")

        heartbeat = 0
        err_count = 0

        while True:
            try:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if today != self.today:
                    if self.today and self.daily_trades > 0:
                        self._log(f"📅 Live 日结 | ${self.daily_pnl:+.4f} ({self.daily_trades}笔)")
                    self.today = today
                    self.daily_trades = 0
                    self.daily_pnl = 0

                self._sync_bot_orders()
                self._settle_all_due()
                self._cleanup_old_windows()

                _, _, end_dt = self._window_ts()
                secs_left = (end_dt - datetime.now(timezone.utc)).total_seconds()
                in_entry = self.cfg.entry_window_end < secs_left < self.cfg.entry_window_start

                for coin in cfg.coins:
                    try:
                        self._check_and_trade(coin)
                    except Exception as e:
                        self._log(f"⚠️ LIVE {coin.upper()} check 异常: {e}")

                heartbeat += 1
                interval = cfg.fast_poll_interval if in_entry else cfg.slow_poll_interval
                beats_per_10m = int(600 / interval)
                if heartbeat % beats_per_10m == 0:
                    wr = (self.total_wins / self.total_count * 100) if self.total_count else 0
                    self._log(
                        f"💓 LIVE ${self.total_pnl:+.4f} | {self.total_count}笔 WR={wr:.0f}% "
                        f"| Pending={self._managed_pending_orders_count()} OpenPos={self._active_live_positions_count()} Reserved=${self._reserved_budget():.2f} "
                        f"| {self.prices.source_status()}"
                    )
                    self._save_state()

                err_count = 0
                time.sleep(interval)

            except KeyboardInterrupt:
                self._log(f"⏹ 手动停止 LIVE | ${self.total_pnl:+.4f} | {self.total_count}笔")
                self._save_state()
                break
            except Exception as e:
                err_count += 1
                self._log(f"❌ LIVE 主循环异常 ({err_count}): {e}")
                if err_count > 20:
                    self._log("连续错误过多，等 60 秒...")
                    time.sleep(60)
                    err_count = 0
                else:
                    time.sleep(5)


def check():
    trader = LiveTrader()
    print("=== Live Trader Check ===")
    print("live_enabled=", trader.live_enabled)
    print("dry_run=", trader.dry_run)
    print("allowed_coins=", sorted(trader.allowed_coins))
    print("max_order_usdc=", trader.max_order_usdc)
    print("max_concurrent_positions=", trader.max_concurrent_positions)
    print("order_timeout_seconds=", trader.order_timeout_seconds)
    print("usdc_balance=", trader._get_usdc_balance())
    print("native_POL=", trader._get_native_pol())
    print("pending_orders=", trader._managed_pending_orders_count())
    print("active_positions=", trader._active_live_positions_count())
    print("reserved_budget=", trader._reserved_budget())
    print("bot_managed_order_ids=", sorted(trader.bot_orders.keys()))
    print("check_result=OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        check()
    else:
        trader = LiveTrader()
        trader.run()
