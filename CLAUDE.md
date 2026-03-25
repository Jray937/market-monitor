# CLAUDE.md

## 專案概述
- **名稱**: market-monitor
- **類型**: Discord Bot（Python 3.11 + discord.py）
- **功能**: 美股/加密貨幣即時監控，自動發送技術分析摘要到 Discord 頻道
- **GitHub**: github.com/Jray937/market-monitor

---

## 技術棧
- Python 3.11
- discord.py（真正 Bot，非 Webhook）
- yfinance（Yahoo Finance，免費數據）
- Polygon.io（期權數據，免費/Pro 版本）
- 技術指標：RSI / MACD / MA200 / 布林帶

## 部署
- Railway（透過 GitHub Actions 自動部署）
- `railway.toml` / `runtime.txt` / `Procfile`

---

## 環境變數（全部在 Railway 設定，不進 GitHub）
| 變數 | 說明 |
|------|------|
| `DISCORD_BOT_TOKEN` | Discord Bot Token |
| `DISCORD_CHANNEL_ID` | 自動摘要目標頻道 ID |
| `POLYGON_API_KEY` | Polygon.io API Key |
| `POLYGON_API_TIER` | `free`（默認）或 `pro` |

---

## 🚨 開發流程鐵律

**每次改完代碼，必須遵守以下順序：**
1. **開分支** → `git checkout -b fix/xxx`
2. **本地測試** → `python -m py_compile bot.py src/*.py`
3. **提交 PR** → `gh pr create`
4. **等 CI/CD 全部通過**（Railway Build + GitHub Actions）
5. **Merge** → `gh pr merge --squash`（只有本人可操作）

**❌ 禁止：直接 push 到 main**
**❌ 禁止：merge 未通過 build 的 PR**

---

## Polygon API 版本邏輯
- **免費版** (`POLYGON_API_TIER=free`)：`list_options_contracts` 返回 `list`，直接迭代
- **Pro 版** (`POLYGON_API_TIER=pro`)：返回 `ListResponse` 對象，用 `.results` 迭代
- 判斷方式：檢查返回值是否有 `.results` 屬性

---

## 主要模組
- `bot.py` — Discord Bot 入口
- `src/` — 主要邏輯模組（analyzer 等）
