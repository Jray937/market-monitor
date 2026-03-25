# Market Monitor

AI 驅動的市場監控 Bot，部署在 **Railway**（單一服務），以 **7 個 Discord Bot** 組成的投研團隊運行。

## 功能

- **技術分析**：RSI、MACD、MA200、布林帶（透過 yfinance）
- **Alert 監控**：自訂規則（超買/超賣/MACD 交叉等），冷卻時間控制
- **多 Bot 協調**：1 個 Leader Bot + 6 個專業 Agent Bot，透過團隊頻道協作
- **定期摘要**：每 4 小時發送分類市場摘要（Stocks / Crypto / Options）
- **LLM 驅動調度**：Leader Bot 用 MiniMax LLM 理解任意輸入，智能調度最合適的 Agent

## 架構

```
用戶 ──► Leader Bot（接收需求，LLM 決策）
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
- Leader Bot = LLM 驅動的智能調度員（MiniMax）
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
git clone https://github.com/Jray937/market-monitor.git
cd market-monitor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
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
| `MINIMAX_API_BASE_URL` | `https://api.minimaxi.com/v1` | ✅ |
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
> **所有 7 個 Bot 都要加入同一個伺服器，並在團隊頻道有讀/發訊息權限。**

## 使用方式

### `/ask`（主力命令）

```
/ask 分析NVDA的技術面和行業前景
/ask 比特幣現在風險大嗎？
/ask 宏觀角度看美股後市
/ask 蘋果值得投資嗎？
```

所有問題都經過 LLM 分析，自動調度最合適的 Agent。

### 其他斜線命令

- `/幫助` — 顯示使用說明
- `/團隊` — 查看所有 Agent 成員
- `/ask` — 向團隊提問（主力命令）

### @mention

也可以 `@LeaderBot 你覺得現在納指怎麼樣？`，Bot 會回覆「🤔 分析需求中...」然後調度團隊。

## LLM 驅動調度

Leader Bot 每次收到需求都會調用 MiniMax LLM 分析，決定：

| action | 說明 |
|--------|------|
| `answer` | 簡單問題，Leader 直接回答（不轉發團隊） |
| `dispatch` | 專業分析，調度相關 Agent |
| `hybrid` | 先說結論（direct_answer），再團隊分析 |

**智能調度示例：**
- 「比特幣風險大嗎？」→ `risk_officer` + `intelligence_officer`
- 「分析蘋果技術面」→ `trader` + `sector_analyst`
- 「今天是什麼日子？」→ `answer`（直接回答）

## 狀態追蹤

每個 Agent 有獨立狀態，用戶可即時看到進度：

```
📤 發送中 → ✅ 已接收 → 🔄 處理中 → 📝 匯總中 → ✅ 完成
                ↓（120 秒無回應）
             ❌ 接收超時
```

最終報告顯示每位成員狀態 + 各 Agent 分析內容 + LLM 彙總結論。

## Leader Bot 職責

- 接收用戶需求（DM / @mention / `/ask`）
- LLM 分析意圖，智能調度 Agent
- 監聽團隊頻道，收集 Agent 報告
- 維護狀態追蹤，即時回饋進度
- 彙總結論回覆用戶

## 團隊成員

| Agent | 職責 |
|-------|------|
| 📊 交易員 | 技術分析、进出场点位 |
| 📈 行業研究員 | 基本面、行业、估值 |
| 🌍 宏觀策略師 | 宏觀經濟、政策影響 |
| 📰 情報官 | 新聞、市場情緒 |
| ⚠️ 風控官 | 風險評估、倉位建議 |
| 🔢 量化策略師 | 量化信號、統計分析 |

## 設定

`config.yaml` 控制監控標的與 Alert 規則：

```yaml
monitor:
  interval_minutes: 15
  summary_interval: 60

symbols:
  stocks:
    - symbol: "NVDA"
      alerts:
        - type: "rsi_oversold"
          threshold: 30
          cooldown_hours: 6
  crypto:
    - symbol: "BTC-USD"
      alerts:
        - type: "rsi_overbought"
          threshold: 70
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
├── config.yaml           # 監控標的、Alert、Agent 配置
├── README.md
├── src/
│   ├── discord_bot.py     # 全部 Bot 邏輯（Leader + Team Agents）
│   ├── analyzer.py       # 技術指標計算（RSI/MACD/MA/BB）
│   ├── data_fetcher.py    # yfinance 數據拉取
│   ├── alert_manager.py   # Alert 邏輯與狀態管理
│   ├── config.py          # 設定檔讀取
│   ├── logger.py          # 日誌設定
│   └── options_fetcher.py # 選擇權數據（Polygon.io）
├── shared/
├── docker-compose.yaml
├── Dockerfile
└── railway.toml
```

## 開發

```bash
# 開分支
git checkout -b fix/xxx

# 編譯檢查
python -m py_compile bot.py src/*.py

# 提交 PR
gh pr create

# Merge（等 CI 通過後）
gh pr merge --squash
```

## 常見問題

**Q: 6 個 Agent Bot 都上線了但都不處理任務？**
A: 確認 (1) 所有 7 個 Bot 都在同一個伺服器 (2) 團隊頻道對所有 Bot 開了讀/發權限 (3) 每個 Bot 的 Application 都有 `Message Content Intent` 開啟。

**Q: `/ask` 命令沒反應？**
A: 確認 Bot 已加入伺服器，且在該頻道有發言權限。

## Changelog

### 2026-03-25

- **Leader Bot LLM 化**：廢除正則匹配，改用 MiniMax LLM 理解任意輸入，智能調度 Agent
- **新增 `/ask` 命令**：主力接口，defer 防止超時
- **狀態追蹤重構**：每個 Agent 獨立狀態（發送中→已接收→處理中→完成/超時）
- **Agent 確認回傳**：收到任務後發送 `✅ 已接收任務` 確認
- **配置修復**：`config.yaml` `agents:` 區塊恢復
- **@mention 修復**：改用 ID 比對 `client.user.id in mention_ids`

## 參考

- [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) — Multi-Agent 架構參考
