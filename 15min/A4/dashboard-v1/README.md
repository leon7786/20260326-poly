# A4 / dashboard-v1

A4 的 `dashboard-v1` 是当前 **Polymarket 15 分钟 paper-trading 看板 + trader** 的代码快照。

## 目录定位

- `15min/A4/`：A4 专属工作区
- `15min/A4/dashboard-v1/`：A4 当前看板版本

## 当前文件

- `dashboard.py`
  - Web 看板服务
  - 默认端口：`5011`
  - 展示 7 币种状态、Polymarket 盘口、交易记录、资金曲线头部摘要
- `paper_trader_v3.py`
  - 当前 paper trader 主程序
  - 使用真实市场数据 + dry run 下单逻辑
- `requirements.txt`
  - Python 依赖
- `README.md`
  - 本说明文档

---

## 这个 dashboard 是什么

这不是普通行情页，而是一个 **operator cockpit / bot dashboard**。

目标是快速回答这几个问题：

1. bot 是否在线
2. 当前 7 个币种各自处于什么 round 状态
3. Polymarket 当前盘口和赔率是否可交易
4. 为什么当前回合交易 / 为什么跳过
5. 最近交易结果、PnL、权益变化如何

---

## 监控币种

当前固定监控 7 个币种：

- BTC
- ETH
- SOL
- XRP
- DOGE
- HYPE
- BNB

---

## 数据源架构（当前口径）

### 1) underlying 行情：WSS

当前 trader 已切到以下实时源：

- **Binance WSS**
- **OKX WSS**

用途：
- 获取 underlying 实时价格
- 形成多源价格共识
- 计算开盘后动量、RSI、trend、共识方向

#### Binance WSS

- 地址：`wss://stream.binance.com:9443/stream?streams=<combined_streams>`
- 当前使用：`<coin>usdt@bookTicker`
- 读取字段：`bid / ask`
- 中间价：`(bid + ask) / 2`

#### OKX WSS

- 地址：`wss://ws.okx.com:8443/ws/v5/public`
- 订阅：`tickers`
- symbol 格式：`BTC-USDT` / `ETH-USDT` / ...
- 读取字段：`last`

### 2) Polymarket：真实 market + 真实盘口

当前要求是：

> **交易标的、市场状态、盘口、赔率、结算口径尽量都来自真实 Polymarket 源站；只有下单动作保持 dry run。**

实现方式：

#### Gamma API（仅用于找 market / token）

用途不是拿价格，而是：
- 根据当前 15 分钟窗口 slug
- 动态找到该回合 market
- 解析出 YES/NO 对应 token id

示意：

```python
slug = f"btc-updown-15m-{start_unix}"
GET https://gamma-api.polymarket.com/markets?slug=<slug>
```

#### Polymarket CLOB WSS（真实盘口）

- 地址：`wss://ws-subscriptions-clob.polymarket.com/ws/market`
- 订阅格式：

```json
{
  "type": "Market",
  "assets_ids": ["<TOKEN_ID>"],
  "auth": {}
}
```

用途：
- 获取 YES / NO token 的真实 `bid / ask`
- 获取 book levels / depth
- 获取实时 price changes

因此当前 trader 的买价来源是：

> **Polymarket CLOB WSS 的真实 ask**

而不是本地模拟价格。

---

## 交易逻辑概要

### 核心原则

- **真实源站市场与盘口**
- **dry run / paper trading**
- 不真实下单
- 只记录如果在当时按规则交易会发生什么

### 回合机制

Polymarket 是 15 分钟 Up/Down 市场：

- 每 15 分钟一个窗口
- 窗口开始记录开盘价
- 窗口结束时比较收盘价 vs 开盘价
  - 收盘价 > 开盘价 → `UP` 结算为 `$1`
  - 收盘价 < 开盘价 → `DOWN` 结算为 `$1`

### 当前入场逻辑（简化）

在接近结算时，只要满足这些条件才会 paper buy：

1. 进入入场时间窗
   - `entry_window_start = 150s`
   - `entry_window_end = 10s`
2. 价格相对开盘价的动量超过阈值
   - `momentum_threshold = 0.08%`
3. RSI 不处于极端反向危险区
4. 日度风控未触发
5. Polymarket 真实盘口存在
6. 买入 ask 不高于上限
   - `max_buy_price = 0.75`

### 当前风格

Leon 已明确要求：

> 回到第一版交易思路，只做轻度放宽，不要换成完全另一套过滤逻辑。

所以当前实现仍然是：

- 动量
- RSI
- 临近结算入场
- 真实盘口 ask 过滤
- 概率模型保留为参考，但默认不做硬门槛

---

## round 级日志 / 自学习样本

当前版本不只记录成交，还记录 **round 级全样本日志**。

输出文件（运行时）包括：

- `paper_trades_v3.jsonl`
  - 已成交的 paper trades
- `paper_rounds_v3.jsonl`
  - 每回合样本日志（包括无交易回合）

### round 日志里重点记录

- `open_price`
- `flip_count`
- `flip_times`
- `candidate_side`
- `skip_reason`
- `entry_price`
- `buy_price`
- `consensus_agree / total`
- `entry_rsi`
- `entry_trend`
- `entry_momentum_pct`
- `book depth / spread / levels`
- `won / pnl`

其中 Leon 特别要求保留：

> **每次价格翻转的时间**

所以日志里已经包含：
- `flip_times[]`
  - `ts`
  - `secs_left`
  - `from`
  - `to`
  - `price`
  - `momentum_pct`

---

## 当前状态说明

当前本地运行中的版本已经是：

- **Binance WSS**
- **OKX WSS**
- **Polymarket CLOB WSS**
- `Gamma API` 仅用于动态找当期 token
- 下单仍是 **paper trade**

### 但要注意

当前还不是严格意义上的“全链路毫秒级 event-driven 实盘结构”。

原因：

1. 核心行情源虽然已经 WSS 化
2. 但策略循环仍有调度间隔
3. token 切换发现仍依赖 Gamma HTTP

所以准确描述应为：

> **核心实时行情源已经 WSS 化，正在向更完整的 event-driven 架构推进。**

---

## 运行

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动看板

```bash
python dashboard.py
```

### 启动 trader

```bash
python paper_trader_v3.py
```

---

## 后续建议

A4 目录后续建议继续分层：

```text
A4/
└── dashboard-v1/
    ├── dashboard.py
    ├── paper_trader_v3.py
    ├── requirements.txt
    ├── README.md
    ├── systemd/          # 如需再补 service 文件
    ├── docs/             # 额外说明
    └── snapshots/        # 版本快照
```

如果 Leon 后续要，我下一步可以继续把以下内容也补进 A4：

- `systemd` service 文件
- round logger / sample exporter
- dashboard 截图或结构图
- 更完整的 WSS/event-driven 架构说明
