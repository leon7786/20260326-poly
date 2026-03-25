# A9 / 15min 项目总览

这个目录是 A9 在 `20260326-poly` 仓库里的工作区。

当前这里包含两部分：

- `dashboard-v1/`
  - 当前 5011 看板实现快照
- `trading-system-v1/`
  - 当前 15 分钟 Polymarket crypto 纸交易系统快照

## 当前目标

让 **看板** 和 **交易系统** 完全对齐，而不是各自讲各自的话。

也就是说：

- 看板展示什么
- 交易系统实际如何发现市场、接 WSS、做决策、写日志
- 两边都使用同一套运行产物

必须保持一致。

## 当前对齐原则

### 1. 同一套 symbol universe

当前追踪的 7 个币种：

- BTC
- ETH
- SOL
- XRP
- DOGE
- HYPE
- BNB

### 2. 同一套 15 分钟 round 概念

看板和交易系统都围绕相同的 round 概念工作：

- 每 15 分钟一轮
- 每轮记录开盘价、收盘价、最高价、最低价
- 记录 flip 次数
- 记录每次 flip 时间
- 记录 source quality / spread / momentum / blockers

### 3. 同一套 live 运行文件

当前对齐的 live 文件是：

- `polymarket_crypto_paperbot_multi_live_status.json`
- `polymarket_crypto_paperbot_multi_live_summary.json`
- `polymarket_crypto_paperbot_multi_live_ledger.jsonl`
- `polymarket_crypto_paperbot_multi_live_signals.jsonl`
- `polymarket_crypto_paperbot_multi_live_rounds.jsonl`
- `polymarket_crypto_paperbot_multi_live_flips.jsonl`

看板必须以这些文件为准。

### 4. 同一套策略语义

看板里看到的：

- 命中市场
- 不交易原因
- 最接近成交
- round board
- trade blotter

都必须来自交易系统真实运行输出，而不是前端自己编一套逻辑。

## 当前运行服务

### 看板
- `polymarket-dashboard-5011.service`

### 交易系统
- `polymarket-paperbot-multi.service`

## 关于密钥和地址

A9 的运行信息来源于 VPS 上的本地私有文件（例如 `A9.txt`），但：

- **密钥不会提交到仓库**
- **私钥不会提交到仓库**
- 仓库里只放代码、结构、README、示例配置

如果后面要接入真实交易，也应该通过：

- 本地 `.env`
- systemd 环境变量
- 或单独的 secrets 文件

而不是把敏感信息写进 Git 仓库。

## 当前资金检查结果（简述）

基于链上公开查询：

- 开发者地址约有 **10.05186 USDC**
- relayer signer 地址当前约 **0 USDC**
- 两个地址当前都约 **0 POL**

这说明：

- 你说的“大概 10 美金”这件事，**开发者地址上是成立的**
- 但如果后面要做真正实盘，还需要再确认：
  - 实际用于下单的地址到底是哪一个
  - relayer / signer / trader 之间的职责关系
  - 当前这 10 USDC 是否就是实际下单账户资金

## 目录建议

- `dashboard-v1/`：前台控制台
- `trading-system-v1/`：后台交易系统
- 后续如果继续演进，可以新增：
  - `dashboard-v2/`
  - `trading-system-v2/`
  - `docs/`
  - `schemas/`

当前先把 v1 的真实运行版本固化下来。
