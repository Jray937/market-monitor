# Market Monitor

AI 驅動的市場監控 Bot，部署在 **Railway**（單一服務），以 **7 個 Discord Bot** 組成的投研團隊運行。

## 功能

- **技術分析**：RSI、MACD、MA200、布林帶（透過 yfinance）
- **Alert 監控**：自訂規則（超買/超賣/MACD 交叉等），冷卻時間控制
- **多 Bot 協調**：1 個 Leader Bot + 6 個專業 Agent Bot，透過團隊頻道協作
- **定期摘要**：每 4 小時發送分類市場摘要（Stocks / Crypto / Options）

## 架構

```
用戶 ──► Leader Bot（接收需求）
              │
              ▼ 發任務到「團隊頻道」
         ┌────┴────┬──────┬──────┬──────┬──────┐
         ▼         ▼      ▼      ▼      ▼      ▼
     📊 交易員  📈 行業  🌍 宏觀  📰 情報官  ⚠️ 風控官  🔢 量化
     (trader)  (sector) (macro) (intel) (risk)  (quant)
              │
              ▼ 各 Agent 回傳報告到團隊頻道
         Leader Bot 彙整結論回覆用戶
```

- **單一 Railway 服務**，7 個 Bot 以 Python threading 同時運行
- 各 Bot 使用各自獨立的 Discord Token
- 透過共享「團隊頻道」以訊息格式協調（無需額外訊息代理）

## 快速開始

### 前置需求

- Python 3.11+
- Discord Bot Tokens（7 個，見下方）
- [MiniMax API Key](https://api.minimax.cn/)（中國區相容 Anthropic SDK）
- Railway 帳號（部署用）

### 本地開發

```bash
# 1. 克隆
git clone https://github.com/Jray937/market-monitor.git
cd market-monitor

# 2. 建立虛擬環境
python -m venv .venv
source .venv/bin/activate

# 3. 安裝依賴
pip install -r requirements.txt

# 4. 複製環境變數範例
cp .env.example .env
# 編輯 .env，填入所有 Token

# 5. 本地測試
python bot.py
```

### 部署到 Railway

1. Fork/Clone 本 repo 到 GitHub
2. 在 Railway 新增 Project，連接到你的 GitHub repo
3. 設定所有環境變數（見下方）
4. Railway 自動部署 `bot.py` → 7 個 Bot 同時上線

## 環境變數

| 變數 | 說明 | 必要 |
|------|------|------|
| `LEADER_BOT_TOKEN` | Leader Bot 的 Discord Token | ✅ |
| `TEAM_CHANNEL_ID` | 團隊協調用的 Discord 頻道 ID | ✅ |
| `MINIMAX_API_KEY` | MiniMax API Key | ✅ |
| `MINIMAX_API_BASE_URL` | `https://api.minimaxi.com/anthropic` | ✅ |
| `MINIMAX_MODEL` | `MiniMax-M2.7` | ✅ |
| `CONFIG_PATH` | `config.yaml` 位置，預設 `/app/config.yaml` | |
| `HTTPS_PROXY` / `HTTP_PROXY` | 代理（如需要） | |

**各 Agent Bot Token**（全部必要，否則該 Bot 不會啟動）：

| 變數 | 對應 Agent |
|------|-----------|
| `CHIEF_STRATEGIST_TOKEN` | 首席策略師 |
| `TRADER_TOKEN` | 交易員 |
| `SECTOR_ANALYST_TOKEN` | 行業研究員 |
| `MACRO_STRATEGIST_TOKEN` | 宏觀策略師 |
| `INTELLIGENCE_OFFICER_TOKEN` | 情報官 |
| `RISK_OFFICER_TOKEN` | 風控官 |
| `QUANT_STRATEGIST_TOKEN` | 量化策略師 |

> 💡 這些 Token 來自 [Discord Developer Portal](https://discord.com/developers/applications)，每個 Bot 需要单独建立 Application 並取得 Token。

## 使用方式

### 發起分析

在 Discord 中 **DM Leader Bot** 或 **@mention** 它：

```
@LeaderBot 分析 NVDA
```

Leader 會回覆「任務已分發」，並在團隊頻道廣播任務。5 分鐘後彙整所有 Agent 報告回傳結論。

### 斜線命令

- `/幫助` — 顯示使用說明
- `/團隊` — 查看所有 Agent 成員

## 設定

`config.yaml` 控制監控標的與 Alert 規則：

```yaml
monitor:
  interval_minutes: 15
  summary_interval: 60   # 每 4 小時（60 分鐘 × 1 小時）

symbols:
  stocks:
    - symbol: "NVDA"
      alerts:
        - type: "rsi_oversold"    # RSI < 30
          threshold: 30
          cooldown_hours: 6
        - type: "rsi_overbought"   # RSI > 70
          threshold: 70
          cooldown_hours: 6
  crypto:
    - symbol: "BTC-USD"
      alerts:
        - type: "rsi_oversold"
          threshold: 30
          cooldown_hours: 4

agents:
  trader:
    enabled: true
    token_env: "TRADER_TOKEN"
    watch_symbols: ["NVDA", "TSLA"]
```

## 專案結構

```
market-monitor/
├── bot.py                 # 入口點
├── config.yaml            # 監控標的、Alert、Agent 配置
├── src/
│   ├── discord_bot.py     # 全部 Bot 邏輯（Leader + Team Agents）
│   ├── analyzer.py        # 技術指標計算（RSI/MACD/MA/BB）
│   ├── data_fetcher.py    # yfinance 數據拉取
│   ├── alert_manager.py   # Alert 邏輯與狀態管理
│   ├── config.py          # 設定檔讀取
│   ├── logger.py          # 日誌設定
│   └── options_fetcher.py # 選擇權數據（Polygon.io）
├── shared/                # 跨 Module 共享工具
├── docker-compose.yaml    # 本地 Docker 部署
├── Dockerfile
└── railway.toml           # Railway 部署設定
```

## 開發

```bash
# 開分支
git checkout -b fix/xxx

# 編譯檢查
python -m py_compile bot.py src/*.py

# 提交 PR（等 CI 通過後 Merge）
gh pr create
```

## 參考

- [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) — 類似 Multi-Agent 架構參考
