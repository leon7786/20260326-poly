# A9 / live mode roadmap

这个文档描述从当前 paper-trading 系统走向真实交易系统时，需要完成的关键步骤。

## 当前阶段

当前系统仍处于：

- WSS-first market data
- Polymarket short-horizon market discovery
- paper trading
- live dashboard / operator console

## 已有前提

- dashboard-v1 已存在
- trading-system-v1 已存在
- live runtime 文件已形成
- A9 开发者地址链上公开可见约有 10 USDC

## 仍需确认的关键点

### 1. 实际执行地址到底是谁

当前至少涉及：

- relayer signer
- developer address
- potential trader address

必须确认：

- 下单最终使用哪个地址
- 10 USDC 是否就在最终执行地址上
- relayer/signer/trader 各自角色是什么

### 2. Gas / POL

当前公开查询显示：

- 相关地址的原生 POL 余额约为 0

如果某条执行链路需要 gas，必须先补足。

### 3. Allowance / approvals / relayer capability

需要确认：

- 是否已有足够授权
- relayer key 是否可正常下单
- signer 是否有权限代表真实交易地址执行

### 4. 风控和限额

真实交易前必须先固化：

- 最大单笔金额
- 最大同时持仓数
- 每轮最多交易数
- 日亏损停止阈值
- source health 最低门槛

## 建议路径

### Phase 1

继续用 paper mode，把：

- source 稳定性
- ledger continuity
- dashboard honesty
- blockers / decision logic

修到更稳。

### Phase 2

做 live-mode dry-run：

- 保留真实 market discovery
- 保留真实 WSS 数据
- 下单接口只做签名/检查，不真正广播

### Phase 3

启用小额真实单：

- 以约 10 USDC 的小额度开始
- 严格限制仓位
- 保留完整审计日志

## 当前原则

- 先确认地址与资金路径
- 先确认 secrets 注入方式
- 先确认是否需要 POL gas
- 不因为“能做”就跳过这些验证
