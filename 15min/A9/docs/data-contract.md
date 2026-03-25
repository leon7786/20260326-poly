# A9 / data contract

这个文档说明 dashboard 和 trading system 之间共享的数据契约。

目标：

- 看板展示和策略输出完全对齐
- 避免前端自己发明一套和策略不一致的解释

## 当前核心 live 文件

- `polymarket_crypto_paperbot_multi_live_status.json`
- `polymarket_crypto_paperbot_multi_live_summary.json`
- `polymarket_crypto_paperbot_multi_live_ledger.jsonl`
- `polymarket_crypto_paperbot_multi_live_signals.jsonl`
- `polymarket_crypto_paperbot_multi_live_rounds.jsonl`
- `polymarket_crypto_paperbot_multi_live_flips.jsonl`

## 1. status.json

主要用来驱动：

- bot online/offline
- liveMarketsFound
- stats
- current decisions
- perSymbolLearning
- current rounds / latestCompleted rounds

## 2. summary.json

主要用来驱动：

- 当前周期总结
- stats 汇总快照
- chosenMarkets 摘要
- notes

## 3. ledger.jsonl

事件类型重点包括：

- `open`
- `close`

dashboard 使用原则：

- `open` / `close` 作为交易事件流
- 不应该把所有历史 `open` 都显示成当前持仓
- 当前真实持仓应以最新 `stats.openTrades` 为准

## 4. signals.jsonl

常见事件类型包括：

- `status_report`
- `open_decision`
- `settle_decision`
- 其他运行级信号事件

dashboard 可将其用于：

- activity feed
- first trade watch
- 决策解释

## 5. rounds.jsonl

主要承载：

- 每 15 分钟一轮的 round summary
- open / close / high / low
- flipCount
- flip timeline
- source quality
- sample coverage

## 6. flips.jsonl

主要承载：

- 每次价格翻转的时间点
- 方向
- moveBps
- secondsFromRoundOpen

## 对齐原则

### 看板应该信什么

看板应优先信：

1. `live_status.stats`
2. `live_ledger` 中真实存在的交易事件
3. `live_signals` 中真实存在的决策/状态事件
4. `live_rounds` / `live_flips` 中真实 round 结构

### 看板不应该做什么

- 不应该把历史 `open` 自动解释成当前持仓
- 不应该回退显示 unrelated old ledger
- 不应该假设某笔 trade 一定已平仓，除非 ledger/status 已给出明确结果

## 下一步建议

后续可以补：

- JSON schema
- 各字段必填/可选说明
- 版本号
- dashboard 与 bot 的兼容矩阵
