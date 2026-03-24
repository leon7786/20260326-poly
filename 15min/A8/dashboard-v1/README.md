# A8 Dashboard v1 — Polymarket 15min 实时看板 + Paper Trader

## 项目概述

Polymarket 15 分钟周期加密货币 Up/Down 市场的实时看板 + 自动化 Paper Trading 系统。

监控 7 个币种：**BTC, ETH, SOL, XRP, DOGE, HYPE, BNB**

---

## 文件结构

```
dashboard-v1/
├── README.md                 ← 本文件
├── paper_trader_v3.py        ← 核心：Paper Trader v3.2（WSS 行情 + 自动结算）
├── dashboard.py              ← Web 看板（Flask，端口 5011）
├── round_logger.py           ← 15 分钟轮次数据记录器
├── requirements.txt          ← Python 依赖
└── systemd/                  ← systemd user service 文件
    ├── polymarket-paper.service
    ├── polymarket-dashboard.service
    └── polymarket-round-logger.service
```

---

## 架构图

```
                    ┌─────────────────────────────────────┐
                    │         WSS 实时行情层               │
                    │                                     │
  Binance WSS ────► │                                     │
  Coinbase WSS ───► │    WSPriceAggregator                │
  OKX WSS ────────► │    (asyncio + threading)            │
  Bybit WSS ──────► │                                     │
                    │         │ thread-safe dict           │
  Polymarket ─────► │    WSS 盘口 (bid/ask)               │
  CLOB WSS         │                                     │
                    └─────────┬───────────────────────────┘
                              │
                              ▼
                    ┌─────────────────────────────────────┐
                    │       Paper Trader v3.2              │
                    │                                     │
                    │  ┌── 概率模型 ──┐                    │
                    │  │ momentum    │                    │
                    │  │ RSI         │──► 胜率 ≥ 80%?     │
                    │  │ trend       │    买入价 ≤ $0.70?  │
                    │  │ consensus   │         │          │
                    │  └─────────────┘         ▼          │
                    │                    🎯 入场 / ⏭ 跳过  │
                    │                         │          │
                    │                    自动结算 (15min)  │
                    └─────────┬───────────────────────────┘
                              │
                              ▼
                    ┌─────────────────────────────────────┐
                    │       Dashboard (port 5011)          │
                    │       Round Logger                   │
                    └─────────────────────────────────────┘
```

---

## WSS 连接详情

### 1. Binance

```
URL:  wss://stream.binance.com:9443/stream?streams=btcusdt@ticker/ethusdt@ticker/...
协议: 合并流（Combined Streams）
数据: 24hr ticker，解析 data.c 字段（close price）
心跳: websockets 库内置 ping/pong（20s 间隔）
重连: 指数退避 2s → 4s → 8s → ... → 60s
```

### 2. Coinbase

```
URL:  wss://ws-feed.exchange.coinbase.com
协议: 连接后发送 subscribe 消息
订阅: {"type":"subscribe","channels":[{"name":"ticker","product_ids":["BTC-USD","ETH-USD","SOL-USD","XRP-USD","DOGE-USD"]}]}
数据: ticker 消息，解析 price 字段
注意: HYPE 和 BNB 在 Coinbase 不可用，graceful skip
```

### 3. OKX

```
URL:  wss://ws.okx.com:8443/ws/v5/public
协议: 连接后发送 subscribe 消息
订阅: {"op":"subscribe","args":[{"channel":"tickers","instId":"BTC-USDT"}, ...]}
数据: tickers 推送，解析 data[0].last
```

### 4. Bybit

```
URL:  wss://stream.bybit.com/v5/public/spot
协议: 连接后发送 subscribe 消息
订阅: {"op":"subscribe","args":["tickers.BTCUSDT","tickers.ETHUSDT", ...]}
数据: tickers 推送，解析 data.lastPrice
```

### 5. Polymarket CLOB

```
URL:  wss://ws-subscriptions-clob.polymarket.com/ws/market
协议: 连接后动态订阅
订阅: {"type":"subscribe","channel":"market","assets_ids":["<token_id>"]}
数据: orderbook 更新，解析 bids/asks 获取 best bid/ask
触发: 当 paper_trader 获取到新市场的 token_id 时，自动通过 WSS 订阅
```

### REST Fallback

当 WSS 某源断线超过 30 秒，自动切换到 REST API 轮询：
- Binance: `GET /api/v3/ticker/price`
- Coinbase: `GET /v2/prices/{sym}-USD/spot`
- OKX: `GET /api/v5/market/ticker`
- Bybit: `GET /v5/market/tickers`
- Polymarket: `GET /book?token_id=...`

---

## 交易策略逻辑

### 市场机制

Polymarket 的 15 分钟 Up/Down 市场：
- 每 15 分钟一个窗口（:00, :15, :30, :45）
- 窗口开始时记录开盘价
- 窗口结束时，如果收盘价 > 开盘价 → UP 合约结算为 $1.00
- 如果收盘价 < 开盘价 → DOWN 合约结算为 $1.00
- 另一方合约结算为 $0.00

### 入场条件（全部满足才下单）

```python
1. 时间窗口:  结算前 840s ~ 10s（入场窗口）
2. 动量阈值:  |momentum| ≥ 0.02%（排除噪音）
3. 胜率模型:  win_probability ≥ 80%
4. 买入价格:  buy_price ≤ $0.70（风控上限）
5. 日限额:    daily_trades < 80 且 daily_pnl > -$15
6. 盘口存在:  strict_real_odds = True（必须有真实盘口）
```

### 概率模型 `estimate_win_probability()`

输入 5 个因子，输出 0.50 ~ 0.995 的胜率估计：

| 因子 | 说明 | 权重 |
|------|------|------|
| **momentum_pct** | 当前价格相对开盘价的变动百分比 | 主因子：0.02%→52%, 0.05%→92%, 0.10%→97%, 0.20%→99% |
| **seconds_to_settlement** | 距结算剩余秒数 | 时间越近越确定（time_factor 0.75~1.0） |
| **trend_strength** | 线性回归斜率（30 个价格点） | 趋势 bonus 最多 +3% |
| **consensus_ratio** | 多少个价格源方向一致 | 共识 bonus ±3% |
| **RSI** | 14 周期相对强弱指数 | 超买(>75)/超卖(<25) 时 penalty 最多 -10% |

```
最终概率 = base × time_factor + trend_bonus + consensus_bonus - rsi_penalty
         = clamp(0.50, 0.995)
```

### 买入价获取优先级

```
1. WSS 盘口 → Polymarket CLOB WSS 实时推送（最快）
2. REST 盘口 → GET /book?token_id=... （WSS 无数据时）
3. Gamma API → GET /markets?slug=...（兜底，可能价格不准）
```

### 结算逻辑

- **主动结算**: 每个循环检查所有已过期窗口（start_unix + 900s + 5s 缓冲）
- 取当时的 median price 作为收盘价
- 计算 PnL：
  - 赢: `+(1.0 - buy_price) × bet_size`
  - 输: `-(buy_price) × bet_size`

### 套利检测

同时检查 UP 和 DOWN 两个 token 的 ask 价格：
```
if ask_UP + ask_DOWN < $0.95:
    套利信号！利润 = $1.00 - (ask_UP + ask_DOWN)
```

---

## 部署

### 依赖

```bash
pip install httpx websockets flask
```

### systemd 服务（user 级别）

```bash
# 复制 service 文件
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload

# 启用并启动
systemctl --user enable --now polymarket-paper
systemctl --user enable --now polymarket-dashboard
systemctl --user enable --now polymarket-round-logger

# 开机自启（需要 linger）
loginctl enable-linger $USER
```

### 端口

| 服务 | 端口 |
|------|------|
| Dashboard | 5011 |

---

## 数据文件

| 文件 | 说明 |
|------|------|
| `output/paper_trades_v3.jsonl` | 每笔交易记录（JSON Lines） |
| `output/paper_trader.log` | Paper Trader 运行日志 |
| `output/dashboard.log` | Dashboard 运行日志 |
| `output/round_logger.log` | Round Logger 运行日志 |
| `data/paper_state_v3.json` | 持久化状态（累计 PnL、胜率等） |

---

## 当前运行状态

- **版本**: v3.2 (WSS Multi-Source + Auto-Settle)
- **累计 PnL**: $+27.56
- **总交易**: 60 笔
- **胜率**: ~80%
- **币种**: BTC, ETH, SOL, XRP, DOGE, HYPE, BNB
- **价格源**: 4/4 WSS + Polymarket CLOB WSS
