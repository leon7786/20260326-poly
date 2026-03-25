# A9 runtime secrets example

> 这是示例文件，只用于说明字段结构。
> 不要把真实密钥、私钥、API 凭证提交到 GitHub。

## 建议本地保存的敏感信息

### Relayer

- `POLY_RELAYER_API_KEY=sk-xxxxxxxxxx`
- `POLY_RELAYER_SIGNER_ADDRESS=0xREDACTED_SIGNER_ADDRESS`

### Trader / Developer

- `POLY_TRADER_ADDRESS=0xREDACTED_TRADER_ADDRESS`
- `POLY_TRADER_API_KEY=sk-xxxxxxxxxx`
- `POLY_PRIVATE_KEY=0xREDACTED_PRIVATE_KEY`

## 建议注入方式

不要把真实 secrets 写进仓库。

推荐：

- `.env`（本地，不提交）
- systemd `EnvironmentFile=`
- 单独 secrets 文件

## 建议占位规则

- API key → `sk-xxxxxxxxxx`
- 私钥 → `0xREDACTED_PRIVATE_KEY`
- 地址 → `0xREDACTED_...`
