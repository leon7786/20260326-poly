# README — polymarket-paper.service

## 作用
systemd user service，启动 paper trader。

## ExecStart
`python3 -u paper_trader_v3.py`

## 特点
- 自动重启
- 日志输出到 paper trader log
