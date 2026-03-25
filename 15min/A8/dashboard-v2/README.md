# A8 Dashboard v2 — Polymarket 15min 看板 + Paper Trader + Live Trader

## 本目录内容

```
dashboard-v2/
├── README.md
├── .env.live.example          ← live 环境变量模板（示例占位符）
├── paper_trader_v3.py         ← WSS paper trader v3.2
├── README-paper_trader_v3.md
├── live_trader.py             ← live trader（带 order lifecycle / timeout cancel / reconcile）
├── README-live_trader.md
├── live_sanity_check.py       ← live 只读联通检查
├── README-live_sanity_check.md
├── dashboard.py               ← Web 看板
├── README-dashboard.md
├── round_logger.py            ← 轮次记录器
├── README-round_logger.md
├── test_wss.py                ← WSS quick test
├── README-test_wss.md
├── requirements.txt
└── systemd/
    ├── polymarket-paper.service
    ├── README-polymarket-paper.service.md
    ├── polymarket-dashboard.service
    ├── README-polymarket-dashboard.service.md
    ├── polymarket-round-logger.service
    ├── README-polymarket-round-logger.service.md
    ├── polymarket-live.service
    └── README-polymarket-live.service.md
```

## v2 相比 v1 的主要新增

### 1. Live Trader
新增 `live_trader.py`，基于当前 15 分钟策略接入 Polymarket CLOB Level 2 auth。

### 2. Live Execution Lifecycle
v2 重点补了实盘执行闭环：
- 下单后先记录 bot-managed `order_id`
- 未成交挂单只算 pending，不算持仓
- 只有真实成交（filled / partial fill）才进入 position / pnl
- timeout cancel（默认 20s）
- startup reconcile：重启后只同步 bot 自己记录的订单
- 不会接管或取消用户历史手动单

### 3. Live env 方案
新增 `.env.live.example`：
- 所有 secret 字段留空
- 只保留默认风控参数
- 真实使用时复制为 `.env.live`

## live 相关环境变量

```bash
POLYMARKET_PRIVATE_KEY=
POLYMARKET_API_KEY=
POLYMARKET_RELAYER_API_KEY=
POLYMARKET_CHAIN_ID=137
POLYMARKET_WALLET_ADDRESS=
POLYMARKET_FUNDER_ADDRESS=

LIVE_ENABLED=false
DRY_RUN=true
LIVE_BUDGET_USDC=10
MAX_ORDER_USDC=1
MAX_CONCURRENT_POSITIONS=1
ALLOWED_COINS=BTC,ETH,SOL
MAX_BUY_PRICE=0.30
MAX_DAILY_LOSS_USDC=2
ORDER_MODE=maker
ORDER_TIMEOUT_SECONDS=20
SYNC_INTERVAL_SECONDS=5
```

## 使用建议

### 先做只读检查
```bash
cd src
python3 live_sanity_check.py
python3 live_trader.py check
```

### 再决定是否开实盘
默认建议：
- `LIVE_ENABLED=false`
- `DRY_RUN=true`

当联通、余额、allowance 都确认无误后，再改为：
- `LIVE_ENABLED=true`
- `DRY_RUN=false`

## systemd

```bash
systemctl --user daemon-reload
systemctl --user enable --now polymarket-paper
systemctl --user enable --now polymarket-dashboard
systemctl --user enable --now polymarket-round-logger
systemctl --user enable --now polymarket-live
```

## 说明
- 不要把真实 `.env.live` 提交到 GitHub
- `.env.live.example` 仅作为模板
- 若使用 signer + funder/profile 结构，资金通常在 funder/profile 地址上
