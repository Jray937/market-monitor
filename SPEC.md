# Market Monitor Bot — 規格書

## 1. 目標與功能

**核心功能**：美股 + 加密貨幣市場監控機器人，自動做技術分析，達到設定條件時第一時間發送 Telegram 通知。

**支援市場**：
- 美股（股票、ETF、ADR）：AAPL, TSLA, NVDA, SPY, QQQ, BTC, ETH 等
- 加密貨幣：BTC, ETH, SOL, BNB 等主流幣

---

## 2. 技術棧

| 層面 | 技術 |
|------|------|
| 語言 | Python 3.11+ |
| 數據源 | Yahoo Finance (yfinance) — 完全免費 |
| 技術分析 | pandas-ta（RSI, MACD, MA, BB） |
| 通知 | python-telegram-bot v20 |
| 部署 | Railway / Render / VPS + Docker |
| 定時任務 | APScheduler（境內輪詢） |

---

## 3. 技術分析指標

### 3.1 趨勢類（Trend）
- **SMA 20 / SMA 50 / SMA 200**：簡單移動平均線，判斷長期趨勢
- **EMA 12 / EMA 26**：指數移動平均線，更敏感
- 策略：當收盤價站上 200 日均線 → 多頭訊號；跌破 → 空頭訊號

### 3.2 動量類（Momentum）
- **RSI（14）**：超買（>70）→ 回落風險；超賣（<30）→ 反彈機會
- **MACD（12,26,9）**：MACD 線上穿信號線 → 金叉（多）；下穿 → 死叉（空）

### 3.3 波動類（Volatility）
- **布林帶（20,2）**：價格觸及上軌 → 強勢；觸及下軌 → 弱勢；突破框架 → 波動放大

---

## 4. 警報邏輯

每個 Symbol 獨立配置閾值，支援以下警報類型：

| 警報類型 | 觸發條件 | 說明 |
|----------|----------|------|
| `price_cross_ma200` | 收盤價上穿/下穿 MA200 | 趨勢反轉 |
| `rsi_overbought` | RSI > 70 持續 N 根 | 超買警告 |
| `rsi_oversold` | RSI < 30 持續 N 根 | 超賣警告 |
| `macd_cross_up` | MACD 上穿信號線 | 金叉 |
| `macd_cross_down` | MACD 下穿信號線 | 死叉 |
| `bollinger_upper` | 價格觸碰布林上軌 | 強勢突破 |
| `bollinger_lower` | 價格觸碰布林下軌 | 弱勢跌破 |
| `rsi_divergence` | 價格創新低但 RSI 未創新低 | 底背離（反彈訊號）|

**冷卻時間**：同一警報 6 小時內不重複觸發（防轟炸）

---

## 5. 系統架構

```
┌──────────────────────────────────────────────┐
│                  Scheduler                    │
│           (APScheduler, 每 N 分鐘)            │
└────────────────┬─────────────────────────────┘
                 │
┌────────────────▼─────────────────────────────┐
│              MarketMonitor                    │
│  ┌──────────────┐  ┌──────────────────────┐  │
│  │ DataFetcher   │  │ TechnicalAnalyzer   │  │
│  │ (yfinance)    │  │ (pandas-ta)          │  │
│  └──────────────┘  └──────────────────────┘  │
│         │                   │                │
│         └─────────┬──────────┘                │
│                   ▼                           │
│         ┌─────────────────┐                   │
│         │ AlertGenerator  │                   │
│         │ (比對閾值)       │                   │
│         └────────┬────────┘                   │
│                  ▼                            │
│         ┌─────────────────┐                   │
│         │ TelegramBot     │                   │
│         │ (python-telegram)│                   │
│         └─────────────────┘                   │
└───────────────────────────────────────────────┘
```

---

## 6. 訊息格式

### 6.1 警報訊息（Alert）
```
📊 [NVDA] 📈 新多頭訊號

價格：$875.42 (+2.3%)
RSI（14）：28.5 ← 【超賣】
MACD：金叉 ✅
MA200：$820（收盤價高於均線）

🕐 2026-03-24 03:55 UTC
```

### 6.2 每小時摘要（Hourly Summary）
```
📋 市場摘要 · 2026-03-24 03:00 UTC

✅ 多頭訊號：NVDA, BTC
⚠️  超買警告：TSLA (RSI=72)
🔴 空頭訊號：ETH
⚪ 無訊號：SPY, QQQ, BNB
```

---

## 7. 配置檔案（config.yaml）

```yaml
telegram:
  bot_token: "YOUR_BOT_TOKEN"
  chat_id: "YOUR_CHAT_ID"

monitor:
  interval_minutes: 15    # 檢查間隔
  summary_interval: 60    # 摘要報告間隔（分鐘）

symbols:
  stocks:
    - symbol: "NVDA"
      alerts:
        - type: "rsi_oversold"
          threshold: 30
          cooldown_hours: 6
        - type: "price_cross_ma200"
          cooldown_hours: 12
    - symbol: "AAPL"
      alerts:
        - type: "macd_cross_up"
          cooldown_hours: 6

  crypto:
    - symbol: "BTC-USD"
      alerts:
        - type: "rsi_oversold"
          threshold: 30
          cooldown_hours: 4
        - type: "rsi_overbought"
          threshold: 70
          cooldown_hours: 4
    - symbol: "ETH-USD"
      alerts:
        - type: "bollinger_lower"
          cooldown_hours: 6
```

---

## 8. 部署方式

### 方案 A：Railway（推薦，持久運行）
- 支援 Python 環境，免費額度最大
- 缺點：免費版有睡眠（idle 50分鐘後休眠，馬上 wake）

### 方案 B：Render（免費，會休眠）
- 免費版 90 天後自動刪除，需注意
- 休眠後 Wake 時間約 30 秒

### 方案 C：VPS + Docker（最穩定）
- 任何有 Docker 的 VPS（AWS EC2, 甲骨文免費, 騰訊雲, 搬瓦工）
- 7×24 全天候運行，無休眠問題

---

## 9. 數據源說明

### Yahoo Finance（yfinance）
- **費用**：完全免費
- **更新延遲**：美股 ~15 分鐘，crypto ~5 分鐘
- **API 限制**：每秒最多 2 個請求（無 key）
- **涵蓋**：股票、ETF、期貨、加密貨幣、外匯

### 替代付費數據源（未來可擴展）
- Alpha Vantage（每天 25 次，免費版）
- Finnhub（每秒 1 次，免費版）
- Polygon.io（付費）
- Coinbase API（加密貨幣，免費）

---

## 10. 警報冷卻機制

- 每個 Symbol + AlertType 獨立計時
- Redis（或本地記憶）記錄上次觸發時間
- 冷卻期內相同警報不重發
- 摘要報告仍會顯示該 Symbol 最新狀態

---

## 11. 項目結構

```
market-monitor/
├── SPEC.md
├── config.yaml              # 設定檔（Symbol、閾值、Token）
├── requirements.txt          # pip 依賴
├── Dockerfile
├── docker-compose.yaml
├── bot.py                    # 入口：排程 + 主迴圈
├── src/
│   ├── __init__.py
│   ├── config.py             # 讀取 config.yaml
│   ├── data_fetcher.py       # yfinance 數據獲取
│   ├── analyzer.py           # 技術指標計算（pandas-ta）
│   ├── alert_manager.py       # 警報狀態 + 冷卻邏輯
│   ├── telegram_bot.py        # Telegram 通知發送
│   └── logger.py              # 日誌設定
└── run.sh                    # 本地啟動腳本
```

---

## 12. 驗收標準

- [x] 美股（股票）和加密貨幣均可監控
- [x] 至少支援：RSI、MACD、MA200、布林帶
- [x] 達到警報條件 → Telegram 收到訊息（< 1 分鐘）
- [x] 警報冷卻機制正常（6 小時內不重複）
- [x] 每小時摘要正常發送
- [x] config.yaml 改 symbol / 閾值無需改代碼
- [x] Docker 容器化，一鍵部署
