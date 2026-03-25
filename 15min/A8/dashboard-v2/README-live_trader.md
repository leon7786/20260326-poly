# README — live_trader.py

## 作用
Polymarket live trader，复用 paper trader 的信号逻辑，但接入真实 CLOB 下单。

## 关键特性
- bot-managed order registry
- pending order / filled position 分离
- timeout cancel
- startup reconcile
- 只同步 bot 自己记录的订单
- 不接管用户历史手动单

## 模式
- dry-run：只签名/模拟，不真实下单
- live：真实提交订单

## 依赖文件
- `.env.live`
- `data/live_state.json`
- `output/live_events.jsonl`
- `output/live_trades.jsonl`

## 检查
```bash
python3 live_trader.py check
```

## 启动
```bash
python3 live_trader.py
```
