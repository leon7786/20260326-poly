# trading-system-v1

这个文件夹保存 A9 当前版本的 15 分钟 Polymarket crypto 纸交易系统。

## 目的

这个系统的职责是：

1. 发现当前 live Polymarket 短周期市场
2. 连接实时 WSS 数据源
3. 生成多源领先价格
4. 记录每 15 分钟 round 结构
5. 按规则决定是否开 paper trade
6. 在 round 结束后结算并写入 live 产物

## 当前主文件

- `polymarket_crypto_paperbot_multi.js`
  - 当前核心交易策略脚本
- `run_polymarket_crypto_paperbot_multi_service.sh`
  - 当前连续运行 runner 脚本

## 当前策略 universe

- BTC
- ETH
- SOL
- XRP
- DOGE
- HYPE
- BNB

## 数据源方向

当前系统在往 **WSS-first** 推进。

### Polymarket

实时盘口：

- `wss://ws-subscriptions-clob.polymarket.com/ws/market`

说明：

- Polymarket 短周期 market 的 token ID 会轮换
- 所以当前 market / token discovery 仍需要动态 metadata 查询
- 但实时盘口/买卖价主要走 CLOB WSS

### Binance

当前方向：

- WSS 实时市场数据
- 用于构建 leader price

### OKX

当前方向：

- WSS 实时市场数据
- 用于构建 leader price

### Coinbase

当前方向：

- WSS 实时市场数据
- 用于构建 leader price

## 当前核心逻辑

### 1. `findLiveRoundMarkets`

负责发现当前 live 的 Polymarket 短周期合约。

### 2. `LeaderCompositeTracker`

负责把多源价格组合成 leader price。

主要记录：

- sourceCount
- sourceSpreadBps
- momentum 5 / 15 / 60
- 最新 composite 价格

### 3. `RoundTracker`

负责记录 round 数据。

每个 round 记录：

- openPx
- closePx
- highPx
- lowPx
- flipCount
- flips
- flipSecondsFromOpen
- source coverage
- spread 分布
- sample 数量
- openingDirection / closingDirection

### 4. `estimateWinProb`

基于当前结构和实时信号估算胜率。

### 5. `evaluateMarket`

评估某个 market 当前是否 eligible。

### 6. `maybeOpenTrades`

符合条件时开 paper trade。

### 7. `maybeSettleTrades`

round 完结后结算 paper trades，并写入 ledger。

## 当前输出文件

交易系统当前写入这一组 live 文件：

- `polymarket_crypto_paperbot_multi_live_status.json`
- `polymarket_crypto_paperbot_multi_live_summary.json`
- `polymarket_crypto_paperbot_multi_live_ledger.jsonl`
- `polymarket_crypto_paperbot_multi_live_signals.jsonl`
- `polymarket_crypto_paperbot_multi_live_rounds.jsonl`
- `polymarket_crypto_paperbot_multi_live_flips.jsonl`

这些文件同时也是 dashboard-v1 的主要数据输入。

## 与看板的对齐要求

这个交易系统必须和 `dashboard-v1` 保持完全对齐。

具体要求：

- 当前命中的 market，要能在看板里看到
- 当前不交易的 blocker，要能在看板里看到
- 当前 7 币 round 统计，要能在看板里看到
- 开仓 / 平仓 / 事件流，要能在看板里看到
- 看板不能额外发明一套和策略不一致的语义

## runner 运行方式

当前 VPS 上使用：

- `polymarket-paperbot-multi.service`

对应 runner：

- `run_polymarket_crypto_paperbot_multi_service.sh`

## 敏感信息处理原则

A9 真实运行所需的：

- relayer key
- signer 地址
- trader 地址
- 私钥

都不应该写进 Git 仓库。

仓库中只保留：

- 策略代码
- runner 脚本
- README
- 示例配置

## 后续建议

下一步如果继续把这个项目工程化，建议加上：

- `.env.example`
- `config.example.json`
- live file schema 文档
- open/close/round/signal 结构说明
- deployment README
