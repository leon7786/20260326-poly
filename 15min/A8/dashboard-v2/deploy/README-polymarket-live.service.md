# README — polymarket-live.service

## 作用
systemd user service，启动 live trader。

## 风险提示
- 只有在 `.env.live` 配置正确且明确要开实盘时才启用
- 推荐先跑 `live_trader.py check`

## ExecStart
`python3 -u live_trader.py`
