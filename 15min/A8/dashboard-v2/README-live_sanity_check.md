# README — live_sanity_check.py

## 作用
只读联通检查脚本。

## 检查内容
- signer / funder 地址
- Polygon 余额
- CLOB health / auth
- allowance
- 真实 market / token / orderbook
- dry-run 构造订单（不提交）

## 用法
```bash
python3 live_sanity_check.py
```

## 安全性
- 不发送真实订单
- 不更新 allowance
- 不做链上写操作
