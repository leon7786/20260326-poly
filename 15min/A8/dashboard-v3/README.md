## 版本说明

- `dashboard-v3` 基于 `dashboard-v2` 复制，并同步了截至 2026-03-25 的本地最新可运行版本。
- 当前 live execution 为 `market` / `FAK` 成交优先模式。
- 目录结构保持 flat：脚本在根目录，systemd 文件在 `systemd/`。

# A8 Dashboard v2 — Polymarket 15min 看板 + Paper Trader + Live Trader

## 项目概述

这是 `dashboard-v1` 的升级版，核心目标是把 **WSS 实时行情 + 看板 + paper trader + live trader** 放到同一个可交接、可部署、可备份的目录中。

v2 的重点不是只多一个 live 文件，而是把 **实盘执行闭环（execution lifecycle）** 补完整：
- 下单记录
- 订单状态跟踪
- timeout cancel
- startup reconcile
- bot-managed order registry
- 只统计 bot 自己的单，不碰用户历史手动单

---

## 本目录内容

```bash
dashboard-v2/
├── README.md
├── .env.live.example
├── paper_trader_v3.py
├── README-paper_trader_v3.md
├── live_trader.py
├── README-live_trader.md
├── live_sanity_check.py
├── README-live_sanity_check.md
├── dashboard.py
├── README-dashboard.md
├── round_logger.py
├── README-round_logger.md
├── test_wss.py
├── README-test_wss.md
├── requirements.txt
└── systemd/
    ├── polymarket-paper.service
    ├── README-polymarket-paper.service.md
    ├── polymarket-dashboard.service
    ├── README-polymarket-dashboard.service.md
    ├── polymarket-round-logger.service
    ├── README-polymarket-round-logger.service.md
    ├── polymarket-live.service
    └── README-polymarket-live.service.md
```

---

## v2 相比 v1 的主要新增

### 1. Live Trader
新增 `live_trader.py`，基于当前 15 分钟策略接入 Polymarket CLOB Level 2 auth。

### 2. Live Execution Lifecycle
v2 重点补了实盘执行闭环：
- 下单后先记录 bot-managed `order_id`
- 未成交挂单只算 pending，不算持仓
- 只有真实成交（filled / partial fill）才进入 position / pnl
- timeout cancel（默认 20s）
- startup reconcile：重启后只同步 bot 自己记录的订单
- 不会接管或取消用户历史手动单

### 3. Live env 方案
新增 `.env.live.example`：
- 敏感字段用占位符示例
- 不放真实密钥
- 真实使用时复制为 `.env.live`

---

## 环境变量设计（live）

### 文件
- 示例模板：`.env.live.example`
- 实际使用：`.env.live`

### 示例

```bash
POLYMARKET_PRIVATE_KEY=0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
POLYMARKET_API_KEY=pk-xxxxxxxxxxxxxxxx
POLYMARKET_RELAYER_API_KEY=rk-xxxxxxxxxxxxxxxx
POLYMARKET_CHAIN_ID=137
POLYMARKET_WALLET_ADDRESS=0x1111111111111111111111111111111111111111
POLYMARKET_FUNDER_ADDRESS=0x2222222222222222222222222222222222222222

LIVE_ENABLED=false
DRY_RUN=true
LIVE_BUDGET_USDC=10
MAX_ORDER_USDC=1
MAX_CONCURRENT_POSITIONS=1
ALLOWED_COINS=BTC,ETH,SOL
MAX_BUY_PRICE=0.30
MAX_DAILY_LOSS_USDC=2
ORDER_MODE=maker
ORDER_TIMEOUT_SECONDS=20
SYNC_INTERVAL_SECONDS=5
```

---

## 部署顺序（推荐）

### Step 1 — 安装依赖
```bash
pip install -r requirements.txt
pip install py-clob-client eth-account web3
```

### Step 2 — 先跑 WSS 检查
```bash
python3 test_wss.py
```

### Step 3 — 配置 live env（如果要开实盘）
```bash
cp .env.live.example .env.live
chmod 600 .env.live
```
然后手动填写真实 key / address。

### Step 4 — 先做只读检查
```bash
python3 live_sanity_check.py
python3 live_trader.py check
```

### Step 5 — 先启 paper / dashboard / logger
```bash
systemctl --user daemon-reload
systemctl --user enable --now polymarket-paper
systemctl --user enable --now polymarket-dashboard
systemctl --user enable --now polymarket-round-logger
```

### Step 6 — 最后才启 live
默认建议先：
- `LIVE_ENABLED=false`
- `DRY_RUN=true`

确认链路正确后，再改为：
- `LIVE_ENABLED=true`
- `DRY_RUN=false`

然后：
```bash
systemctl --user enable --now polymarket-live
```

---

## 启动 / 停止 / 查看命令

### Dashboard
```bash
systemctl --user start polymarket-dashboard
systemctl --user stop polymarket-dashboard
systemctl --user restart polymarket-dashboard
systemctl --user status polymarket-dashboard
```

### Paper Trader
```bash
systemctl --user start polymarket-paper
systemctl --user stop polymarket-paper
systemctl --user restart polymarket-paper
systemctl --user status polymarket-paper
```

### Round Logger
```bash
systemctl --user start polymarket-round-logger
systemctl --user stop polymarket-round-logger
systemctl --user restart polymarket-round-logger
systemctl --user status polymarket-round-logger
```

### Live Trader
```bash
systemctl --user start polymarket-live
systemctl --user stop polymarket-live
systemctl --user restart polymarket-live
systemctl --user status polymarket-live
```

### 查看日志
```bash
journalctl --user -u polymarket-live -n 100 --no-pager
journalctl --user -u polymarket-paper -n 100 --no-pager
journalctl --user -u polymarket-dashboard -n 100 --no-pager
journalctl --user -u polymarket-round-logger -n 100 --no-pager
```

---

## 故障排查 Checklist

### 1. WSS 没连接上
检查：
- `test_wss.py` 是否通过
- VPS 网络是否正常
- 交易所 / Polymarket endpoint 是否可访问
- 日志里有没有重连循环

### 2. Dashboard 打不开
检查：
- `polymarket-dashboard.service` 是否 running
- 5011 端口是否监听
- VPS / 防火墙 / 云平台安全组是否放行
- `/healthz` 是否正常

### 3. Live check 失败
检查：
- `.env.live` 是否存在
- signer / funder 地址是否填对
- API key / private key 是否匹配
- `live_sanity_check.py` 是否通过

### 4. 有余额但 live 不能下单
检查：
- collateral 是否在 `funder/profile` 地址
- allowance 是否已经就绪
- `MAX_BUY_PRICE` 是否太低
- 白名单币种是否限制住了
- `LIVE_ENABLED` / `DRY_RUN` 是否配置正确

### 5. 订单发出但没有成交
这是正常情况的一部分。v2 已支持：
- pending order
- timeout cancel
- startup reconcile
- 只有 filled 才记为 position

### 6. 手动单和 bot 单混在同一账户
v2 方案是：
- bot 只认自己记录过的 `order_id`
- 不接管旧的手动单
- 不取消非 bot-managed 订单

---

## Live 风险提示

### 1. 先 dry-run，再实盘
强烈建议流程：
1. `live_sanity_check.py`
2. `live_trader.py check`
3. `DRY_RUN=true`
4. 观察无误后再开启 `LIVE_ENABLED=true`

### 2. 不要把真实 `.env.live` 提交到 GitHub
仓库里只能放：
- `.env.live.example`

不能放：
- 真私钥
- 真 API key
- 真 relayer key

### 3. signer / funder/profile 结构要分清
常见情况：
- `signer`：签名地址
- `funder/profile`：真正放 collateral 的地址

### 4. Relayer key 不是基础下单必需项
当前基础 CLOB 下单、查单、撤单通常不依赖 relayer key；
但未来做这些时会有价值：
- redeem
- split / merge
- convert
- gasless relayer operations

### 5. live 版本仍应持续观察
v2 已经补上 order lifecycle，但实盘永远应小仓位启动、逐步观察。

---

## 说明
- 不要把真实 `.env.live` 提交到 GitHub
- `.env.live.example` 仅作为模板
- 若使用 signer + funder/profile 结构，资金通常在 funder/profile 地址上
- v2 的重点是：**让 live 可运行，也更可审计、可恢复、可控**
