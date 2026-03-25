# A9 / deploy

这个目录用于保存 A9 15min 项目的部署说明和服务模板。

## 当前内容

- `systemd/polymarket-dashboard-5011.service.example`
  - dashboard 示例服务模板
- `systemd/polymarket-paperbot-multi.service.example`
  - trading system 示例服务模板

## 说明

这些是 **example** 模板，不包含私密信息。

实际部署时需要按机器环境调整：

- `WorkingDirectory`
- `ExecStart`
- 环境变量
- secrets 注入方式

## 推荐部署方式

### dashboard

职责：
- 提供 5011 控制台网页
- 读取 live 运行文件
- 展示当前 bot 状态、历史成交、活动流、round board

### trading system

职责：
- 发现 current live market
- 连接实时 WSS 数据源
- 持续运行 paper/live 策略
- 写入 live runtime 文件

## secrets 原则

不要把：

- API key
- 私钥
- relayer key
- signer 密钥

直接写进 service 文件。

推荐方式：

- systemd `EnvironmentFile=`
- 本地 `.env`
- 单独 secrets 文件

## 目录建议

实际部署时推荐类似结构：

- `/opt/a9-15min/dashboard-v1/`
- `/opt/a9-15min/trading-system-v1/`
- `/opt/a9-15min/runtime/`
- `/opt/a9-15min/logs/`
- `/opt/a9-15min/.env`

这样 dashboard 和 trading system 可以共享 runtime 输出，但职责保持分离。
