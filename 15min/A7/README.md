# A7 专属交易系统 — Agent 优化版 (Dashboard v1 + Paper Trader v3.2)

## 目录概述

这是专门由 AI Agent（OpenClaw）维护并持续优化的策略目录，基于 A8 的原始版本演进而长。

与 A8 原始版本的核心区别在于，这里集成了 **“踩界增频”** 与 **资金管理** 逻辑，并在前端展示上做了定制。

---

## Agent 定制优化点

### 1. “踩界增频”策略 (`🚀[扫单]`)
原始策略对买入价格死守 `$0.70` 的红线，导致大量高胜率（临近结算、方向确立）的机会被跳过（Skip）。在 A7 中，引入了动态滑点机制：
*   **触发条件**：距离结算时间 $\le$ 180 秒，且模型计算胜率 $\ge$ 95%。
*   **阈值放宽**：将可接受的最高买入价（max_buy_price）从 `$0.70` 临时放宽至 **`$0.85`**。
*   **极高确定性扫单**：若此时价格动量剧烈（$\ge$ 0.10%）且四大价格源（Binance, Coinbase, OKX, Bybit）100% 共识，系统将优先吃掉盘口流动性，并在日志中打上 `🚀[扫单]` 标记。
*   **大币种敏感度提升**：针对波动率较低的 **BTC** 和 **ETH**，将入场的动量阈值下调至 **0.015%**（其它币种保持 0.02%），以便更早捕捉趋势启动。

### 2. 真实资金对账逻辑
*   加入了 **初始资金设置（\$10.00）**。
*   所有计算、日志输出及看板顶部展示，均从原始的“仅展示 PnL”，升级为“**实时余额 = 初始资金 + PnL**”。
*   **兜底风控**：如果实时余额跌破单次下注金额（`bet_size` = $1.0），策略会自动熔断，不再开仓，防止纸交易数据失真。

### 3. 看板 UI 微调
*   顶部状态栏新增醒目的加粗余额展示。
*   Favicon 从“🎱”替换为“**7**”，以便在多开标签页时与 A8 区分。

---

## 包含文件

```
15min/A7/
├── README.md                 ← 本文件
├── paper_trader_v3.py        ← 核心策略：带初始资金与动态扫单功能
├── dashboard.py              ← 5011 端口看板：新增余额展示，修改图标
├── requirements.txt          ← Python 依赖
└── systemd/                  ← 针对 A7 部署优化的系统服务配置
    ├── polymarket-dashboard.service
    └── polymarket-paper.service
```

---

## 部署说明

如果你想将 A7 跑在其他服务器上，可以按以下步骤：

```bash
# 1. 建立虚拟环境并安装依赖
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 2. 复制并启用服务
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-dashboard.service
sudo systemctl enable --now polymarket-paper.service

# 3. 访问看板
# http://<你的IP>:5011
```

## 后续迭代方向 (TODO)

*   **多仓并行机制**：目前每个 15min 窗口单币种仅交易一次，后续可考虑在第一笔建立底仓后，若趋势极度强化，允许金字塔式补仓。
*   **实盘对接 (Live Trader)**：在 Paper Trading 数据积累足够（比如胜率稳定在 85% 以上且胜出笔数达 100+）后，将 A7 逻辑无缝切换为实盘签名模式。
