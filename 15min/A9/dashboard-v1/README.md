# A9 / dashboard-v1

这个文件夹存放 A9 当前版本的看板网站打包内容，对应 15 分钟 Polymarket crypto paper-trading 系统的前台控制台。

## 文件夹归属

- `15min/A9/` 是 A9 在这个仓库里的工作目录
- `15min/A9/dashboard-v1/` 存放当前看板实现快照

## 文件说明

- `polymarket_dashboard_5011.js`
  - 当前正在使用的 Node.js 看板服务脚本
- `README.md`
  - 当前看板、WSS 接法、交易逻辑的中文说明

## 这个看板是什么

这不是一个普通行情页，而是一个面向操作者的 **bot cockpit / 交易控制台**。

它主要回答这几个问题：

1. bot 现在是否在线？
2. 当前在盯哪些 live Polymarket 合约？
3. 最近发生了哪些成交和运行事件？
4. 为什么策略成交，或者为什么拒绝成交？
5. 当前最接近下一笔交易的是哪一个 setup？

## 当前页面结构

### 1）顶部 KPI 区

用于快速看核心状态，例如：

- Bot ONLINE / OFFLINE
- 当前 mode
- Paper NAV
- Realized PnL
- Last Fill

### 2）Trade Blotter

只展示真实的 paper `open / close` 成交。

当前表格会展示类似信息：

- 交易时间
- symbol
- market / contract
- side
- stake
- shares / price
- balance
- realized pnl

### 3）Activity feed

展示非成交型运行事件，例如：

- status report
- execution attempt
- decision activity
- early-exit 风格信号

这样即使成交不多，也能看到 bot 最近到底在做什么。

### 4）Matched markets

展示当前 scanner 实际命中的 live Polymarket 合约。

常见字段包括：

- symbol
- 合约 question
- end time
- liquidity
- 24h volume

### 5）No-trade decisions

展示为什么 bot **没有交易**。

主要字段包括：

- direction
- estimated win probability
- round quality
- 距离上次 flip 的时间
- blockers

### 6）7-symbol round board

追踪当前这 7 个币种：

- BTC
- ETH
- SOL
- XRP
- DOGE
- HYPE
- BNB

展示内容包括：

- open price
- move bps
- flip count
- last flip time
- flip timeline
- momentum 5 / 15 / 60
- source spread
- source count
- 当前 decision 状态

### 7）First trade watch

用来高亮当前**最接近成交**的 setup。

这一块的目标是直接告诉操作者：

- 当前最强 setup 是谁
- 估算胜率是多少
- ask / lag / flips / quality 现在怎样
- 还差哪些 blocker 才会真正开仓

## 时区设置

页面时间统一按：

- `GMT+8 / Asia/Shanghai`

交易时间和状态时间都按这个时区显示，避免 UTC 和本地时间混淆。

## 看板的数据来源

看板目前主要依赖两类数据。

## A. bot 写到磁盘的 runtime 文件

当前主要使用这些文件：

- `polymarket_crypto_paperbot_multi_live_status.json`
- `polymarket_crypto_paperbot_multi_live_summary.json`
- `polymarket_crypto_paperbot_multi_live_ledger.jsonl`
- `polymarket_crypto_paperbot_multi_live_signals.jsonl`
- `polymarket_crypto_paperbot_multi_live_rounds.jsonl`
- `polymarket_crypto_paperbot_multi_live_flips.jsonl`

这些文件负责驱动：

- bot 在线/离线状态
- live status 快照
- paper NAV 和 realized pnl
- trade blotter
- decisions / blockers
- rounds / flips 数据

## B. 用于市场展示的实时 WSS 数据

看板本身还会打开实时市场 websocket，用于 watchlist 和市场侧信息展示。

### Polymarket

CLOB 市场 websocket：

- `wss://ws-subscriptions-clob.polymarket.com/ws/market`

用途：

- YES / NO 实时 order book
- best ask 跟踪
- matched market 展示上下文

### Binance

当前看板 watchlist 使用：

- `wss://stream.binance.com:9443/ws/<symbol>@bookTicker`

### Coinbase

当前看板 watchlist 使用：

- `wss://advanced-trade-ws.coinbase.com`

### OKX

当前看板 watchlist 使用：

- `wss://ws.okx.com:8443/ws/v5/public`

### 可选辅助显示源

当前看板代码里也预留了：

- Kraken WSS
- Bybit WSS

这些属于展示侧辅助数据，不是核心交易真相来源。

## Polymarket 市场匹配方式

看板和 bot 都需要拿到当前短周期 live Polymarket 合约。

高层流程如下：

1. 发现当前 live market
2. 解析当前 YES / NO token ID
3. 用这些 token ID 订阅 Polymarket CLOB WSS
4. 跟踪实时盘口
5. 在页面里展示当前 matched contracts

## 交易策略概要

这个看板后面接的是一个 15 分钟 crypto paper-trading 策略。

### 当前交易 universe

当前追踪的 symbol 包括：

- BTC
- ETH
- SOL
- XRP
- DOGE
- HYPE
- BNB

### 策略思路

这是一个短周期方向性策略，核心依赖：

- 多个 CEX 价格作为 leading indicator
- Polymarket 实时盘口 / 定价
- round 级别的结构、momentum、flip 特征
- 足够严格的质量过滤后再开仓

### 当前 bot 的核心逻辑模块

- `findLiveRoundMarkets`
  - 发现当前短周期 Polymarket live 合约

- `LeaderCompositeTracker`
  - 组合 CEX 领先价格
  - 跟踪 source count / source spread
  - 跟踪短周期 momentum

- `RoundTracker`
  - 记录每个 15 分钟 round 的结构
  - open / close / high / low
  - flip count 和 flip 时间
  - source-quality 统计

- `estimateWinProb`
  - 根据 lag / momentum / agreement / round quality 估算胜率

- `evaluateMarket`
  - 评估某个 market 当前是否 eligible

- `maybeOpenTrades`
  - 满足条件时才开 paper trade

- `maybeSettleTrades`
  - round 结束后结算 paper trade，并把事件写入 ledger

## WSS 架构说明

### 当前方向

整个系统正在往更强的 **WSS-first** 架构推进。

### CEX 侧

当前方向是：

- Binance → 实时 WSS 市场数据
- OKX → 实时 WSS 市场数据
- Coinbase → 实时 WSS 市场数据

目标是尽量使用基于 bid/ask 推导的实时价格，而不是把 last / ask / snapshot 混在一起。

### Polymarket 侧

- 实时盘口主要通过 CLOB WSS 获取
- 当前 market / token 发现仍需要依赖 metadata lookup，因为短周期 token ID 会随 round 轮换

重要限制：

- Polymarket 短周期 market 的 token ID 不是固定不变的
- 所以 token discovery 还不能完全纯 websocket
- 当前系统仍需要动态刷新 market metadata

## 当前运行方式

当前 VPS 上的服务包括：

- `polymarket-dashboard-5011.service`
  - 在端口 `5011` 提供看板网站

- `polymarket-paperbot-multi.service`
  - 持续运行 paper-trading bot loop

## 本地运行方式

典型入口：

```bash
node polymarket_dashboard_5011.js
```

生产环境一般通过 systemd 管理，而不是直接 shell 启动。

## 当前设计原则

- 不伪造成交
- 不伪造在线状态
- 不把过期 ledger 假装成当前结果
- 操作者优先
- 交易详情优先
- blocker / no-trade reason 要足够透明

## 当前把这份内容放进仓库的目的

这份 repo 快照主要是为了保存：

- 当前看板网站代码
- 当前页面结构
- 当前 WSS 连接方式
- 当前交易系统的操作逻辑
- 当前服务运行方式

## 后续可能演进方向

`dashboard-v1` 后面可能继续升级到：

- 更强的 trade drill-down
- 点开一笔单后直接解释为什么赢/输
- 更连续的 cumulative NAV 视图
- 更清楚的 source-health diagnostics
- 更干净的 activity 分类
- 更明确的 risk / execution summary
