#!/usr/bin/env python3
"""WSS 连接测试 — 启动 WSPriceAggregator，等 15 秒，打印状态"""
import sys
import time
sys.path.insert(0, ".")
from paper_trader_v3 import WSPriceAggregator

print("🧪 WSS 连接测试开始...")
agg = WSPriceAggregator(coins=["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"])

print("等待 15 秒收集数据...\n")
for i in range(15):
    time.sleep(1)
    connected = sum(1 for s in ["binance", "coinbase", "okx", "bybit"] if agg._connected.get(s))
    print(f"  [{i+1}s] 已连接 {connected}/4 源", end="")
    if i >= 3:
        # 打印 BTC 价格
        prices = agg.get_prices("btc")
        if prices:
            print(f" | BTC: {prices}", end="")
    print()

print(f"\n{'='*60}")
print(f"📊 最终状态: {agg.source_status()}")
print(f"{'='*60}")

coins = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]
total_sources = 0
for coin in coins:
    prices = agg.get_prices(coin)
    total_sources += len(prices)
    if prices:
        from statistics import median
        m = median(prices.values())
        print(f"  {coin.upper():5s}: ${m:>12,.2f} | 源: {list(prices.keys())}")
    else:
        print(f"  {coin.upper():5s}: 无数据")

print(f"\n{'='*60}")
poly_status = "✅ 已连接" if agg._poly_connected else "❌ 未连接"
print(f"  Polymarket CLOB WSS: {poly_status}")
print(f"  总数据点: {total_sources} (7 币 × 最多 4 源)")

# 判断通过
ok_coins = sum(1 for c in coins if agg.get_prices(c))
if ok_coins >= 5 and total_sources >= 15:
    print(f"\n✅ 测试通过! {ok_coins}/7 币种有数据，{total_sources} 个数据点")
else:
    print(f"\n⚠️ 数据偏少: {ok_coins}/7 币种，{total_sources} 个数据点")
    sys.exit(1)
