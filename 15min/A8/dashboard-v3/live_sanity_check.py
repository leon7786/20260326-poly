#!/usr/bin/env python3
"""
Polymarket Live Sanity Check
- 读取 .env.live
- 验证 signer / funder / API creds
- 检查 Polygon 余额（POL / USDC / USDC.e）
- 检查 Polymarket CLOB health / auth / allowance
- 取一个真实 market 的 token / orderbook / tick size
- 构造一笔 dry-run 订单（只签名，不提交）

⚠️ 不会发送真实订单，不会更新 allowance
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Tuple, Optional

from eth_account import Account
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, OrderArgs, PartialCreateOrderOptions
from py_order_utils.model import POLY_PROXY

BASE_DIR = Path(__file__).parent.parent
ENV_FILE = BASE_DIR / ".env.live"
CLOB_HOST = "https://clob.polymarket.com"
POLYGON_RPC = "https://polygon.drpc.org"
GAMMA = "https://gamma-api.polymarket.com"


ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
]

TOKENS = [
    ("USDC.e", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
    ("USDC",   "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"),
]


def load_env(path: Path) -> Dict[str, str]:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def fmt6(x) -> str:
    try:
        return f"{float(x):.6f}"
    except Exception:
        return str(x)


def get_polygon_balances(funder: str) -> Dict[str, float]:
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC, request_kwargs={"timeout": 15}))
    result = {"rpc_connected": w3.is_connected(), "native_POL": 0.0}
    if not result["rpc_connected"]:
        return result

    result["native_POL"] = float(w3.from_wei(w3.eth.get_balance(Web3.to_checksum_address(funder)), "ether"))
    for label, token in TOKENS:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        bal = c.functions.balanceOf(Web3.to_checksum_address(funder)).call()
        dec = c.functions.decimals().call()
        result[label] = bal / (10 ** dec)
    return result


def pick_live_market_and_token() -> Tuple[Optional[dict], Optional[str], Optional[str]]:
    import httpx
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    ws = (now.minute // 15) * 15
    start = now.replace(minute=ws, second=0, microsecond=0)
    start_unix = int(start.timestamp())
    candidate_slugs = [
        f"btc-updown-15m-{start_unix}",
        f"eth-updown-15m-{start_unix}",
        f"sol-updown-15m-{start_unix}",
    ]

    with httpx.Client(timeout=10) as http:
        for slug in candidate_slugs:
            try:
                data = http.get(f"{GAMMA}/markets", params={"slug": slug}).json()
                if not data:
                    continue
                m = data[0]
                cid = m.get("conditionId")
                if not cid:
                    continue
                c = http.get(f"{CLOB_HOST}/markets/{cid}")
                if c.status_code != 200:
                    continue
                clob = c.json()
                tokens = clob.get("tokens", [])
                if len(tokens) >= 2:
                    return m, tokens[0].get("token_id"), tokens[1].get("token_id")
            except Exception:
                continue

        # fallback：扫 active 市场里最先命中的 15m 币种
        try:
            markets = http.get(f"{GAMMA}/markets", params={"limit": 200, "active": True}).json()
            for m in markets:
                slug = (m.get("slug") or "").lower()
                if any(x in slug for x in ["btc-updown-15m", "eth-updown-15m", "sol-updown-15m"]):
                    cid = m.get("conditionId")
                    if not cid:
                        continue
                    c = http.get(f"{CLOB_HOST}/markets/{cid}")
                    if c.status_code != 200:
                        continue
                    clob = c.json()
                    tokens = clob.get("tokens", [])
                    if len(tokens) >= 2:
                        return m, tokens[0].get("token_id"), tokens[1].get("token_id")
        except Exception:
            pass

    return None, None, None


def main():
    env = load_env(ENV_FILE)
    pk = env["POLYMARKET_PRIVATE_KEY"]
    signer = Account.from_key(pk).address
    funder = env.get("POLYMARKET_FUNDER_ADDRESS") or signer
    chain_id = int(env.get("POLYMARKET_CHAIN_ID", "137"))

    print("=== Live Sanity Check ===")
    print("signer:", signer)
    print("funder:", funder)
    print("signature_type: POLY_PROXY (1)")

    # Polygon balances
    bals = get_polygon_balances(funder)
    print("polygon_rpc_connected:", bals.get("rpc_connected"))
    print("native_POL:", fmt6(bals.get("native_POL", 0)))
    print("USDC.e:", fmt6(bals.get("USDC.e", 0)))
    print("USDC:", fmt6(bals.get("USDC", 0)))

    # Polymarket auth
    client = ClobClient(
        CLOB_HOST,
        chain_id=chain_id,
        key=pk,
        signature_type=POLY_PROXY,
        funder=funder,
    )
    print("clob_health:", client.get_ok())
    print("server_time:", client.get_server_time())

    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    print("api_creds: OK")

    allowance = client.get_balance_allowance(BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=POLY_PROXY,
    ))
    print("balance_allowance:", json.dumps(allowance, ensure_ascii=False))

    # pick a real live token
    market, up_token, down_token = pick_live_market_and_token()
    if not market or not up_token:
        print("market_pick: FAILED")
        return 1

    print("market_slug:", market.get("slug"))
    print("condition_id:", market.get("conditionId"))
    print("up_token:", up_token)
    print("down_token:", down_token)

    book = client.get_order_book(up_token)
    print("orderbook_tick_size:", book.tick_size)
    print("best_bid:", book.bids[0].price if book.bids else None)
    print("best_ask:", book.asks[0].price if book.asks else None)

    # dry-run create order: just sign/build, do not post
    dry_price = float(book.asks[0].price) if book.asks else 0.10
    dry_price = min(max(dry_price, 0.01), 0.30)
    order = client.create_order(
        OrderArgs(
            token_id=up_token,
            price=dry_price,
            size=1.0,
            side="BUY",
        ),
        PartialCreateOrderOptions(
            tick_size=book.tick_size,
            neg_risk=bool(book.neg_risk),
        )
    )
    print("dry_run_signed_order: OK")
    print("dry_order_id:", getattr(order, 'id', None) or getattr(order, 'order_id', None) or 'signed')
    print("RESULT: PASS (no real order posted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
