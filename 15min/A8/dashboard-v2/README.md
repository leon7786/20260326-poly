# A8 Dashboard v2 — 精简版目录

## 设计目标

`dashboard-v2` 现在改成 **精简根目录**：
- 根目录只保留少数核心入口 / 配置文件
- 测试脚本放到 `tests/`
- 说明文档放到 `docs/`
- systemd / 部署文件放到 `deploy/`
- 主程序放到 `app/`

这样根目录不会很乱，交接时也更清晰。

---

## 当前目录结构

```bash
dashboard-v2/
├── README.md
├── .env.live.example
├── requirements.txt
├── app/
│   ├── dashboard.py
│   ├── live_trader.py
│   ├── paper_trader_v3.py
│   └── round_logger.py
├── tests/
│   ├── live_sanity_check.py
│   └── test_wss.py
├── docs/
│   ├── README-dashboard.md
│   ├── README-live_trader.md
│   ├── README-live_sanity_check.md
│   ├── README-paper_trader_v3.md
│   ├── README-round_logger.md
│   └── README-test_wss.md
└── deploy/
    ├── polymarket-dashboard.service
    ├── polymarket-live.service
    ├── polymarket-paper.service
    ├── polymarket-round-logger.service
    ├── README-polymarket-dashboard.service.md
    ├── README-polymarket-live.service.md
    ├── README-polymarket-paper.service.md
    └── README-polymarket-round-logger.service.md
```

---

## 根目录保留的文件

### 1. `README.md`
主说明文件。

### 2. `.env.live.example`
live 配置模板，敏感字段只保留占位符示例。

### 3. `requirements.txt`
Python 依赖。

---

## app/
主程序目录，放运行脚本：

- `dashboard.py` — Web 看板
- `live_trader.py` — live trader
- `paper_trader_v3.py` — paper trader
- `round_logger.py` — 轮次记录器

如果以后要继续精简，甚至可以只保留：
- `app/live_trader.py`
- `app/dashboard.py`

其余脚本作为附属模块继续放子目录。

---

## tests/
测试 / 检查脚本统一放这里：

- `live_sanity_check.py` — 只读联通检查
- `test_wss.py` — WSS quick test

这些都不是主运行文件，所以不应该放在根目录。

---

## docs/
按文件拆分的说明文档：

- `README-dashboard.md`
- `README-live_trader.md`
- `README-live_sanity_check.md`
- `README-paper_trader_v3.md`
- `README-round_logger.md`
- `README-test_wss.md`

如果后续还要继续精简，也可以把这些再收敛成：
- `docs/OPERATIONS.md`
- `docs/RUNBOOK.md`

---

## deploy/
部署与 service 文件：

- `polymarket-dashboard.service`
- `polymarket-live.service`
- `polymarket-paper.service`
- `polymarket-round-logger.service`

以及对应的 service README。

---

## 建议使用方式

### 安装依赖
```bash
pip install -r requirements.txt
```

### 运行主程序
```bash
cd app
python3 dashboard.py
python3 paper_trader_v3.py
python3 live_trader.py check
python3 live_trader.py
python3 round_logger.py
```

### 跑测试脚本
```bash
cd tests
python3 live_sanity_check.py
python3 test_wss.py
```

### 使用 service 文件
把 `deploy/` 里的 service 复制到 systemd user 目录。

---

## live 配置模板

`.env.live.example` 里使用的是示例占位符，例如：

```bash
POLYMARKET_PRIVATE_KEY=0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
POLYMARKET_API_KEY=pk-xxxxxxxxxxxxxxxx
POLYMARKET_RELAYER_API_KEY=rk-xxxxxxxxxxxxxxxx
POLYMARKET_WALLET_ADDRESS=0x1111111111111111111111111111111111111111
POLYMARKET_FUNDER_ADDRESS=0x2222222222222222222222222222222222222222
```

真实使用时：
1. 复制成 `.env.live`
2. 填真实值
3. 不要提交到 GitHub

---

## 说明

这版调整的核心不是删文件，而是把目录结构整理成：
- 根目录简洁
- 主程序集中
- 测试脚本归档
- 文档归档
- 部署文件归档

这样后续你看 `dashboard-v2` 时，不会一眼看到很多散文件。