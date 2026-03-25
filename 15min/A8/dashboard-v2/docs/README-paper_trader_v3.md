# README — paper_trader_v3.py

## 作用
WSS 版 paper trader v3.2。

## 主要功能
- Binance / Coinbase / OKX / Bybit WSS 实时行情
- Polymarket CLOB WSS 盘口
- 15 分钟 Up/Down 策略
- paper 模式自动结算

## 关键风控
- 胜率阈值
- 最大买入价
- 日交易数上限
- 日亏损上限

## 输出
- `output/paper_trades_v3.jsonl`
- `data/paper_state_v3.json`

## 启动
```bash
python3 paper_trader_v3.py
python3 paper_trader_v3.py stats
```
