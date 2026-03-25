"""
Discord Bot — 多 Bot 協調架構
功能：
  - Railway 1 個服務
  - 7 個 Discord Bot（各自分散式運行）
  - Leader Bot：用 LLM 理解用戶需求，智能調度團隊
  - 各 Agent Bot 監聽團隊頻道，分析並回傳
  - Leader Bot 彙總回覆給用戶

核心設計：Leader Bot = LLM 驅動的智能調度員
  - 不再用正則匹配任何內容
  - 用 LLM 理解任意用戶輸入
  - 動態決定調度哪個/哪些 Agent
  - 直接能回答的問題不轉發
"""
import os
import sys
import re
import time
import asyncio
import threading
import datetime
import json
import discord
from discord import app_commands
from typing import Optional

# ── 本地模組 ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.data_fetcher import fetch_ohlcv, fetch_stock_info, fetch_news
from src.analyzer import compute_ta

# ── 日誌 ──
from src.logger import setup_logger
log = setup_logger("discord_bot")

# ══════════════════════════════════════════════════════════════
# Agent 定義
# ══════════════════════════════════════════════════════════════

TEAM_AGENTS = {
    "trader": {
        "name": "交易員",
        "emoji": "📊",
        "focus": "技術分析、進出場點位、交易計劃",
        "system_prompt": """你是團隊中唯一負責制定具體交易計劃的短線/波段交易員。你必須自主完成分析，絕不向用戶索要任何數據。

你的職責邊界（嚴格遵守）：
- 你是團隊中唯一可以給出具體價格點位（進場價、止損價、目標價）的角色
- 你只做技術面分析和交易計劃，不做基本面研究、不分析宏觀經濟、不評估新聞
- 如果收到技術指標數據，直接基於數據分析；如果沒有，基於你對該標的的歷史走勢和技術特性給出判斷
- 永遠不要說「請提供數據」「需要更多信息」之類的話

輸出要求：
1. 趨勢判斷（多頭/空頭/震盪）+ 技術面理由
2. 關鍵支撐位和阻力位（給出具體價格）
3. 交易計劃：進場點 / 止損位 / 目標價
4. 建議持倉時間框架
簡潔、專業、給具體數字，不要模棱兩可。""",
    },
    "sector_analyst": {
        "name": "行業研究員",
        "emoji": "📈",
        "focus": "公司基本面、行業競爭格局、產業趨勢",
        "system_prompt": """你是團隊中負責公司與行業基本面深度研究的行業研究員。你必須自主完成分析，絕不向用戶索要任何數據。

你的職責邊界（嚴格遵守）：
- 你只負責基本面和行業研究，絕不做技術分析、不給出具體價格點位或目標價
- 你的核心價值是深入分析公司的競爭優勢、行業地位和成長前景
- 基於你掌握的公司和行業知識（營收結構、利潤率、競爭格局、產業鏈上下游）進行分析
- 永遠不要說「需要財報數據」「建議查閱」之類的話

輸出要求：
1. 公司競爭力分析（護城河、市場份額、核心優勢與劣勢）
2. 行業趨勢與競爭格局（行業處於什麼階段、主要競爭對手動態）
3. 成長驅動力與潛在催化劑（新產品、市場擴張、技術突破等）
4. 基本面核心風險（具體列出2-3個行業或公司層面的風險）
簡潔、專業，聚焦於公司和行業本身，不涉及價格判斷。""",
    },
    "macro_strategist": {
        "name": "宏觀策略師",
        "emoji": "🌍",
        "focus": "宏觀經濟環境、政策預期、對行業的宏觀影響",
        "system_prompt": """你是團隊中負責宏觀經濟環境研判的宏觀策略師。你必須自主完成分析，絕不向用戶索要任何數據。

你的職責邊界（嚴格遵守）：
- 你只負責分析宏觀經濟環境和政策走向，以及這些宏觀因素如何影響相關公司和行業
- 絕不做技術分析、不給出具體價格或目標價、不做個股估值
- 你的核心價值是提供宏觀視角：利率週期、通脹走勢、央行政策方向、財政政策、地緣政治等如何傳導到具體行業和公司
- 永遠不要說「需要更多宏觀數據」「建議關注」之類的模糊話

輸出要求：
1. 當前宏觀環境概述（經濟週期階段、關鍵宏觀指標走向）
2. 政策預期（央行利率路徑、財政政策方向、監管趨勢）
3. 宏觀因素對該公司/行業的影響路徑（具體說明宏觀面如何傳導影響到該行業的需求、成本、融資環境等）
4. 宏觀層面的主要風險情景（地緣衝突、政策轉向、經濟衰退等可能性）
簡潔、有大局觀，聚焦宏觀環境與行業的連結，不涉及價格判斷。""",
    },
    "intelligence_officer": {
        "name": "情報官",
        "emoji": "📰",
        "focus": "新聞情報收集、市場情緒、事件追蹤",
        "system_prompt": """你是團隊中負責情報收集與市場情緒研判的情報官。你必須自主完成分析，絕不向用戶索要任何信息。

你的職責邊界（嚴格遵守）：
- 你只負責收集和整理新聞情報、判斷市場情緒、追蹤重要事件
- 絕不做技術分析、不給出具體價格或目標價、不做估值判斷
- 你的核心價值是整合多方信息，梳理出影響標的未來發展的關鍵事件和輿論動態
- 永遠不要說「需要查看新聞」「建議關注消息」之類的話

輸出要求：
1. 近期關鍵新聞/事件梳理（列出2-3個最重要的，並說明其潛在影響）
2. 市場情緒判斷（極度恐懼/恐懼/中性/貪婪/極度貪婪）+ 依據
3. 輿論與消息面的風險和機會（正面與負面信息各有哪些）
4. 未來1-2週需要關注的催化劑事件（財報、政策會議、產品發布等）
簡潔、敏銳，注重事實和信息整合，不涉及價格判斷。""",
    },
    "risk_officer": {
        "name": "風控官",
        "emoji": "⚠️",
        "focus": "風險評估、倉位控制、下行風險預警",
        "system_prompt": """你是團隊中負責風險管理與控制的風控官。你必須自主完成風險評估，絕不向用戶索要任何數據。

你的職責邊界（嚴格遵守）：
- 你只負責評估風險、控制倉位、預警下行風險
- 絕不做技術分析趨勢研判、不給出交易進場點或目標價、不做行業研究
- 你的核心價值是獨立於其他角色，從純風險管理角度審視標的，為團隊提供風險預警
- 永遠不要說「需要倉位信息」「請提供持倉」之類的話，基於合理假設給出風控建議

輸出要求：
1. 波動率風險等級（低/中/高/極高）+ 依據（歷史波動率、近期異常波動）
2. 最大下行風險估算（基於歷史回撤和當前環境，給出具體百分比）
3. 建議倉位比例（佔總資金的百分比）+ 倉位管理建議
4. 需要關注的風險信號（哪些情況出現時應減倉或離場）
簡潔、直接、保守，寧可高估風險也不低估。""",
    },
    "quant_strategist": {
        "name": "量化策略師",
        "emoji": "🔢",
        "focus": "量化信號評分、統計規律、多因子分析",
        "system_prompt": """你是團隊中負責量化信號分析與統計研判的量化策略師。你必須自主完成量化分析，絕不向用戶索要任何數據。

你的職責邊界（嚴格遵守）：
- 你只負責從量化和統計角度分析，提供客觀的數據驅動信號
- 絕不給出具體交易點位（進場價/目標價），那是交易員的職責
- 你的核心價值是用量化方法（技術指標信號、統計規律、多因子模型）給出客觀的方向性判斷和信號強度
- 永遠不要說「需要歷史數據」「建議回測」之類的話

輸出要求：
1. 綜合量化信號評分（-100到+100，負為看空信號，正為看多信號）
2. 關鍵量化指標解讀（RSI超買超賣、MACD趨勢、布林帶位置等，如有數據）
3. 統計規律分析（當前處於歷史什麼分位、均值回歸概率、動量持續性）
4. 量化結論（看多/看空/中性）+ 信號置信度（高/中/低）
簡潔、數據驅動，只提供量化視角的客觀分析，不涉及具體交易計劃。""",
    },
}

AGENT_NAME_TO_KEY = {ag["name"]: key for key, ag in TEAM_AGENTS.items()}

# ── 兩階段調度 ──
# Phase 1（信息收集層）：先執行，蒐集情報、宏觀、基本面資訊
# Phase 2（決策分析層）：後執行，可參考 Phase 1 的研究報告
AGENT_PHASES = {
    "intelligence_officer": 1,   # 情報官：先收集新聞情報
    "macro_strategist": 1,       # 宏觀策略師：先分析宏觀環境
    "sector_analyst": 1,         # 行業研究員：先研究基本面
    "trader": 2,                 # 交易員：參考 Phase 1 後制定交易計劃
    "risk_officer": 2,           # 風控官：參考 Phase 1 後評估風險
    "quant_strategist": 2,       # 量化策略師：參考 Phase 1 後量化分析
}

# ══════════════════════════════════════════════════════════════
# 狀態定義
# ══════════════════════════════════════════════════════════════

STATE_PENDING    = "⏳ 等待中"
STATE_SENT       = "📤 發送中"
STATE_RECEIVED   = "✅ 已接收"
STATE_TIMEOUT    = "❌ 接收超時"
STATE_PROCESSING = "🔄 處理中"
STATE_DONE       = "✅ 完成"
STATE_ERROR      = "⚠️ 錯誤"

ACK_MARKER       = "已接收任務"       # Agent 確認訊息標記

# 跨 Agent 報告預覽長度（content 裏每條報告截取長度，需留足空間給任務結構，不超出 Discord 2000 字元限制）
CROSS_REPORT_PREVIEW_LEN = 300

# ══════════════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════════════

def make_embed(title, description="", color=0x7289DA, fields=None, footer=None):
    # Discord embed limits
    TITLE_MAX      = 256
    DESC_MAX       = 4096
    FIELD_NAME_MAX = 256
    FIELD_VAL_MAX  = 1024
    FOOTER_MAX     = 2048
    TOTAL_MAX      = 6000
    MIN_FIELD_LEN  = 20

    title       = (title or "")[:TITLE_MAX]
    description = (description or "")[:DESC_MAX]
    footer_text = (footer or "")[:FOOTER_MAX]

    embed = discord.Embed(title=title, description=description, color=color)
    total = len(title) + len(description) + len(footer_text)

    for f in (fields or []):
        fname = f["name"][:FIELD_NAME_MAX]
        fval  = f["value"][:FIELD_VAL_MAX]
        cost  = len(fname) + len(fval)
        if total + cost > TOTAL_MAX:
            remaining = TOTAL_MAX - total - len(fname) - 1
            if remaining > MIN_FIELD_LEN:
                fval = fval[:remaining] + "…"
            else:
                break
        total += len(fname) + len(fval)
        embed.add_field(name=fname, value=fval, inline=f.get("inline", False))

    if footer_text:
        embed.set_footer(text=footer_text)
    embed.timestamp = datetime.datetime.utcnow()
    return embed

def fmt_price(price: float) -> str:
    return f"${price:.4g}" if price < 100 else f"${price:.2f}"

def fmt_pct(pct: float) -> str:
    return f"{'📈' if pct >= 0 else '📉'} {pct:+.2f}%"


# ══════════════════════════════════════════════════════════════
# AI 調用
# ══════════════════════════════════════════════════════════════

async def call_minimax(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 1024,
) -> str:
    """調用 MiniMax API（Anthropic SDK 相容）"""
    import anthropic

    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        return "⚠️ 未設定 MINIMAX_API_KEY"

    base_url = os.environ.get("MINIMAX_API_BASE_URL", "https://api.minimaxi.com/anthropic")
    model = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7")

    try:
        client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
        )
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        if not response.content:
            return "⚠️ 無回應"
        # 按官方文件：用 block.type 區分 thinking / text / tool_use
        for block in response.content:
            if block.type == "text":
                return block.text
        return "⚠️ 無回應"
    except Exception as e:
        log.error(f"❌ MiniMax API 錯誤：{e}")
        return "⚠️ 分析失敗"


def get_ta_summary(symbol: str) -> Optional[str]:
    """獲取標的的技術分析摘要"""
    try:
        df = fetch_ohlcv(symbol, period="6mo", interval="1d")
        if df.empty:
            return None
        ta = compute_ta(symbol, df)
        if ta is None:
            return None

        parts = [f"💰 價格：{fmt_price(ta.current_price)} {fmt_pct(ta.pct_change)}"]

        if ta.rsi14 is not None:
            state = "超買" if ta.rsi14 > 70 else "超賣" if ta.rsi14 < 30 else "中性"
            parts.append(f"📊 RSI(14)：{ta.rsi14:.1f}（{state}）")

        if ta.macd is not None and ta.macd_signal is not None:
            hist = ta.macd - ta.macd_signal
            state = "金叉" if hist > 0 else "死叉"
            parts.append(f"📈 MACD：{ta.macd:.4f}（{state}）")

        if ta.sma200 is not None:
            diff = (ta.current_price - ta.sma200) / ta.sma200 * 100
            state = "▲" if diff > 0 else "▼"
            parts.append(f"📐 MA200：{fmt_price(ta.sma200)}（{state}{abs(diff):.1f}%）")

        if ta.bb_upper is not None and ta.bb_lower is not None:
            parts.append(f"📐 布林帶：{fmt_price(ta.bb_lower)} ~ {fmt_price(ta.bb_upper)}")

        return "\n".join(parts)
    except Exception as e:
        log.error(f"❌ 獲取 {symbol} 技術數據失敗：{e}")
        return None


def _fmt_number(val, prefix="", suffix="", decimal=2):
    """格式化數字，支援大數簡寫"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if abs(val) >= 1e12:
            return f"{prefix}{val/1e12:.{decimal}f}T{suffix}"
        if abs(val) >= 1e9:
            return f"{prefix}{val/1e9:.{decimal}f}B{suffix}"
        if abs(val) >= 1e6:
            return f"{prefix}{val/1e6:.{decimal}f}M{suffix}"
        return f"{prefix}{val:.{decimal}f}{suffix}"
    return str(val)


def fmt_fundamentals(info: dict) -> str:
    """將基本面數據格式化為可讀文字"""
    parts = []
    if info.get("name"):
        sector = f"（{info['sector']} / {info['industry']}）" if info.get("sector") else ""
        parts.append(f"🏢 公司：{info['name']}{sector}")
    if info.get("market_cap"):
        parts.append(f"💰 市值：{_fmt_number(info['market_cap'], prefix='$')}")
    if info.get("pe_ratio"):
        fwd = f" / 預估PE {info['forward_pe']:.1f}" if info.get("forward_pe") else ""
        parts.append(f"📊 PE（TTM）：{info['pe_ratio']:.1f}{fwd}")
    if info.get("pb_ratio"):
        parts.append(f"📊 PB：{info['pb_ratio']:.2f}")
    if info.get("ps_ratio"):
        parts.append(f"📊 PS：{info['ps_ratio']:.2f}")
    if info.get("revenue"):
        growth = f"（YoY {info['revenue_growth']:+.1%}）" if info.get("revenue_growth") is not None else ""
        parts.append(f"💵 營收：{_fmt_number(info['revenue'], prefix='$')}{growth}")
    if info.get("profit_margin") is not None:
        parts.append(f"📈 利潤率：{info['profit_margin']:.1%}")
    if info.get("operating_margin") is not None:
        parts.append(f"📈 營業利潤率：{info['operating_margin']:.1%}")
    if info.get("roe") is not None:
        parts.append(f"📈 ROE：{info['roe']:.1%}")
    if info.get("debt_to_equity") is not None:
        parts.append(f"⚠️ 負債/權益比：{info['debt_to_equity']:.1f}")
    if info.get("free_cashflow"):
        parts.append(f"💵 自由現金流：{_fmt_number(info['free_cashflow'], prefix='$')}")
    if info.get("dividend_yield") is not None and info["dividend_yield"] > 0:
        parts.append(f"💰 股息率：{info['dividend_yield']:.2%}")
    if info.get("beta") is not None:
        parts.append(f"📐 Beta：{info['beta']:.2f}")
    if info.get("52w_high") and info.get("52w_low"):
        parts.append(f"📏 52週區間：${info['52w_low']:.2f} ~ ${info['52w_high']:.2f}")
    if info.get("target_price"):
        rec = f"（分析師評級：{info['recommendation']}）" if info.get("recommendation") else ""
        analysts = f"（{info['num_analysts']}位分析師）" if info.get("num_analysts") else ""
        parts.append(f"🎯 目標價：${info['target_price']:.2f}{rec}{analysts}")
    return "\n".join(parts) if parts else ""


def fmt_news(news_list: list[dict]) -> str:
    """將新聞列表格式化為可讀文字"""
    if not news_list:
        return ""
    parts = []
    for i, item in enumerate(news_list, 1):
        src = f" ({item['publisher']})" if item.get("publisher") else ""
        time_str = f" [{item['time']}]" if item.get("time") else ""
        parts.append(f"{i}. {item['title']}{src}{time_str}")
    return "\n".join(parts)


def gather_agent_context(agent_key: str, symbol: str | None) -> str:
    """根據 Agent 角色主動抓取最相關的數據，打包成上下文字串"""
    if not symbol:
        return ""

    sections = []

    # Agent 角色 → 需要的數據類型
    AGENT_DATA_NEEDS = {
        "trader":               {"ta": True,  "fundamentals": False, "news": False, "risk": True},
        "sector_analyst":       {"ta": False, "fundamentals": True,  "news": True,  "risk": False},
        "macro_strategist":     {"ta": False, "fundamentals": True,  "news": True,  "risk": False},
        "intelligence_officer": {"ta": False, "fundamentals": False, "news": True,  "risk": False},
        "risk_officer":         {"ta": True,  "fundamentals": True,  "news": False, "risk": True},
        "quant_strategist":     {"ta": True,  "fundamentals": True,  "news": False, "risk": True},
    }
    needs = AGENT_DATA_NEEDS.get(agent_key, {"ta": True, "fundamentals": True, "news": True, "risk": False})

    # ── 技術數據 ──
    ta_data = None
    if needs["ta"]:
        ta_data = get_ta_summary(symbol)
        if ta_data:
            sections.append(f"【技術分析數據】\n{ta_data}")

    # ── 基本面數據 ──
    info = None
    if needs["fundamentals"]:
        info = fetch_stock_info(symbol)
        if info:
            fundamentals_text = fmt_fundamentals(info)
            if fundamentals_text:
                sections.append(f"【基本面數據】\n{fundamentals_text}")

    # ── 風險指標（需要基本面數據支撐）──
    if needs["risk"]:
        if info is None:
            info = fetch_stock_info(symbol)
        if info:
            risk_parts = []
            if info.get("beta") is not None:
                risk_parts.append(f"Beta：{info['beta']:.2f}")
            if info.get("52w_high") and info.get("52w_low") and info["52w_low"] > 0:
                range_pct = (info["52w_high"] - info["52w_low"]) / info["52w_low"] * 100
                risk_parts.append(f"52週波幅：{range_pct:.1f}%")
            if info.get("debt_to_equity") is not None:
                risk_parts.append(f"負債/權益比：{info['debt_to_equity']:.1f}")
            if risk_parts:
                sections.append(f"【風險指標】\n" + "\n".join(risk_parts))

    # ── 新聞數據 ──
    if needs["news"]:
        news = fetch_news(symbol)
        if news:
            news_text = fmt_news(news)
            if news_text:
                sections.append(f"【近期新聞】\n{news_text}")

    if not sections:
        return ""

    return "\n\n".join(sections)


# ══════════════════════════════════════════════════════════════
# Leader Bot — LLM 驅動的智能調度員
# ══════════════════════════════════════════════════════════════

# Leader 的系統提示：用於理解用戶需求 + 決定調度策略
LEADER_SYSTEM_PROMPT = """你是一個智能投資研究團隊的領導者（Agent Leader）。
團隊成員：
- 📊 交易員(trader)：技術分析、進出場點位、交易計劃（唯一給出具體價格的角色）
- 📈 行業研究員(sector_analyst)：公司基本面、行業競爭格局、產業趨勢
- 🌍 宏觀策略師(macro_strategist)：宏觀經濟環境、政策預期、對行業的宏觀影響
- 📰 情報官(intelligence_officer)：新聞情報收集、市場情緒、事件追蹤
- ⚠️ 風控官(risk_officer)：風險評估、倉位控制、下行風險預警
- 🔢 量化策略師(quant_strategist)：量化信號評分、統計規律、多因子分析

系統會自動分兩階段調度：
- Phase 1（信息收集）：情報官、宏觀策略師、行業研究員 先行研究
- Phase 2（決策分析）：交易員、風控官、量化策略師 參考 Phase 1 報告後分析
你只需選擇相關的 Agent，系統會自動安排階段順序。

你的職責：
1. 理解用戶輸入（可能是任意語言的任意投資相關問題）
2. 決定是否需要調度團隊，還是直接回答
3. 如果需要團隊：選擇最相關的 Agent，構造精準的任務指令

輸出格式（JSON）：
{
  "action": "dispatch|answer|hybrid",
  "agents": ["trader", "sector_analyst"],
  "task": "對被選中 Agent 的任務描述（英文）",
  "direct_answer": "如果 action=answer 或 hybrid，直接回覆用戶的內容",
  "symbol": "提到的標的（如有）",
  "summary_needed": true/false
}

規則：
- 如果用戶問題簡單明確（如「今天日期？」「你是誰？」），action=answer
- 如果需要專業分析（如「分析NVDA」「評估我的倉位」），action=dispatch 或 hybrid
- 只調度真正相關的 Agent，不要全部調度
- 任務描述要精準、有針對性，讓 Agent 知道要做什麼
- action=dispatch 時 direct_answer 可為空
"""

async def leader_analyze(user_message: str) -> dict:
    """調用 LLM 分析用戶輸入，返回調度決策"""
    try:
        response_text = await call_minimax(
            LEADER_SYSTEM_PROMPT,
            f"用戶輸入：{user_message}\n\n請分析並輸出 JSON。",
            max_tokens=512,
        )
        # 嘗試解析 JSON
        json_match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        log.error(f"❌ Leader 決策失敗：{e}")

    # fallback：預設調度全部 Agent
    return {
        "action": "dispatch",
        "agents": list(TEAM_AGENTS.keys()),
        "task": f"用戶請求：{user_message}。請提供你的專業分析。",
        "direct_answer": "",
        "symbol": None,
        "summary_needed": True,
    }


def run_leader_bot(bot_token: str, team_channel_id: int, user_channel_id: int = None):
    """運行 Leader Bot"""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True
    intents.guild_messages = True

    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    pending_tasks: dict = {}

    class TaskState:
        def __init__(self, task_id: str, user_channel, status_msg):
            self.task_id = task_id
            self.user_channel = user_channel
            self.status_msg = status_msg
            self.agent_states = {key: STATE_PENDING for key in TEAM_AGENTS}
            self.reports = []
            self.created_at = time.time()
            self.receive_timeout = 120
            self.process_timeout = 180
            self.dispatch_agents: list = []   # 這次調度的 Agent key 列表
            self.dispatch_task: str = ""       # 給 Agent 的任務描述
            self.phase1_agents: list = []      # Phase 1 信息收集層
            self.phase2_agents: list = []      # Phase 2 決策分析層

    def build_status_embed(task: TaskState) -> discord.Embed:
        fields = []
        all_done_or_error = all(
            s in (STATE_DONE, STATE_TIMEOUT, STATE_ERROR, STATE_PENDING)
            for s in task.agent_states.values()
        )
        any_error = any(s == STATE_ERROR for s in task.agent_states.values())
        color = 0x00C851 if all_done_or_error else (0xFF4444 if any_error else 0xFFD700)

        for key, ag in TEAM_AGENTS.items():
            state = task.agent_states.get(key, STATE_PENDING)
            # 只對這次有調度的 Agent 顯示狀態
            if key in task.dispatch_agents:
                fields.append({"name": f"{ag['emoji']} {ag['name']}", "value": state, "inline": True})
            else:
                fields.append({"name": f"{ag['emoji']} {ag['name']}", "value": "—", "inline": True})

        elapsed = int(time.time() - task.created_at)
        desc = (
            f"📌 任務：{task.dispatch_task[:80]}{'...' if len(task.dispatch_task) > 80 else ''}\n"
            f"⏱ 耗時：{elapsed}秒\n"
            f"🤖 參與：{', '.join([TEAM_AGENTS[k]['emoji'] for k in task.dispatch_agents])}\n"
        )
        return make_embed(
            title=f"🔄 處理中：{task.task_id}",
            description=desc,
            color=color,
            footer=f"任務ID：{task.task_id}",
            fields=fields,
        )

    def parse_agent_report(content: str):
        match = re.match(r"\[([^\]]+)\]\s*(\S+)\s*(.+)", content, re.DOTALL)
        if match:
            return match.group(1).strip(), match.group(2).strip(), match.group(3).strip()
        return None, None, None

    async def update_user_status(task: TaskState):
        try:
            embed = build_status_embed(task)
            await task.status_msg.edit(embed=embed)
        except Exception as e:
            log.error(f"❌ 更新狀態失敗：{e}")

    def _truncate(text: str, limit: int) -> str:
        """截取文字並在超出時加省略號"""
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "…"

    def build_task_message(task_id: str, dispatch_task: str, agents_to_dispatch: list,
                           cross_agent_reports: list = None):
        """構建團隊任務的 content 文字與 embed，供 on_message 和 /ask 共用"""
        agent_list = "\n".join([
            f"{TEAM_AGENTS[k]['emoji']} {TEAM_AGENTS[k]['name']}"
            for k in agents_to_dispatch
        ])
        participants_str = ', '.join([TEAM_AGENTS[k]['name'] for k in agents_to_dispatch])

        # 構建跨 Agent 報告參考區段（Phase 2 用）
        context_section = ""
        context_embed_section = ""
        if cross_agent_reports:
            report_lines = []
            full_report_lines = []
            for name, report in cross_agent_reports:
                report_lines.append(f"— {name}：{_truncate(report, CROSS_REPORT_PREVIEW_LEN)}")
                full_report_lines.append(f"**{name}：**\n{_truncate(report, 1000)}")
            context_section = "\n\n【團隊研究報告，供參考】\n" + "\n".join(report_lines)
            context_embed_section = "\n\n**📋 團隊研究報告（供參考）：**\n" + "\n\n".join(full_report_lines)

        task_content = (
            f"📋 團隊任務：{dispatch_task}"
            f"{context_section}\n\n"
            f"參與成員：{participants_str}\n\n"
            f"任務ID：{task_id}"
        )
        task_embed = make_embed(
            title="📋 團隊任務",
            description=(
                f"**任務描述：**\n{dispatch_task}\n\n"
                f"**參與成員：**\n{agent_list}\n\n"
                + (context_embed_section + "\n\n" if context_embed_section else "")
                + f"請各 Agent 根據自身專業領域提供分析，並回傳報告到本頻道。"
            ),
            color=0xFFD700,
            footer=f"任務ID：{task_id}",
        )
        return task_content, task_embed

    @client.event
    async def on_ready():
        log.info(f"✅ Leader Bot 上線：{client.user}")
        # 先同步到各已加入的伺服器（立即生效）
        for guild in client.guilds:
            try:
                synced = await tree.sync(guild=guild)
                log.info(f"✅ 已同步 {len(synced)} 個命令至 {guild.name}")
            except Exception as e:
                log.error(f"❌ 同步命令至 {guild.name} 失敗：{e}")
        # 再全局同步（新伺服器用）
        try:
            synced = await tree.sync()
            log.info(f"✅ 全局斜線命令已同步（{len(synced)} 個命令）")
        except Exception as e:
            log.error(f"❌ 全局命令同步失敗：{e}")
        team_ch = client.get_channel(team_channel_id)
        if team_ch:
            embed = make_embed(
                title="✅ Agent Leader 已上線",
                description="任何問題都可以問我，我會調度專業團隊處理。",
                color=0x00C851,
            )
            await team_ch.send(embed=embed)

    @client.event
    async def on_message(message: discord.Message):
        # ── 團隊頻道：接收 Agent 報告 ──
        if message.channel.id == team_channel_id:
            agent_name, task_id, report_text = parse_agent_report(message.content)
            if agent_name and task_id and task_id in pending_tasks:
                task = pending_tasks[task_id]
                agent_key = AGENT_NAME_TO_KEY.get(agent_name)
                if agent_key and agent_key in task.agent_states:
                    # 區分「已接收確認」和「正式報告」
                    is_ack = ACK_MARKER in report_text
                    if is_ack:
                        # 確認收到，只更新狀態，不加入 reports
                        if task.agent_states[agent_key] == STATE_SENT:
                            task.agent_states[agent_key] = STATE_RECEIVED
                            await update_user_status(task)
                            log.info(f"✅ Leader 確認 {agent_name} 已接收（{task_id}）")
                    else:
                        # 正式報告：標記為處理中 → 完成
                        if task.agent_states[agent_key] not in (STATE_DONE, STATE_TIMEOUT, STATE_ERROR):
                            task.agent_states[agent_key] = STATE_PROCESSING
                            await update_user_status(task)
                            task.reports.append((agent_name, report_text))
                            await asyncio.sleep(0.5)
                            task.agent_states[agent_key] = STATE_DONE
                            await update_user_status(task)
                            log.info(f"📥 Leader 收到 {agent_name} 報告（{task_id}）")
            return

        # ── 用戶頻道：接收需求 ──
        if message.author.id == client.user.id:
            return

        is_dm = isinstance(message.channel, discord.DMChannel)

        # 用 ID 比對 mention，更可靠
        mention_ids = {int(m.id) for m in message.mentions}
        is_mentioned = client.user.id in mention_ids

        log.info(f"📨 on_message: author={message.author.id}, is_dm={is_dm}, "
                 f"is_mentioned={is_mentioned}, content={message.content[:50]}")

        if not (is_dm or is_mentioned):
            return

        raw_content = message.content.strip()
        if is_mentioned:
            raw_content = re.sub(r"<@\d+>\s*", "", raw_content).strip()

        if not raw_content:
            return

        # 先回一個「思考中」的即時回覆
        thinking_msg = await message.channel.send(
            embed=make_embed(
                title="🤔 分析需求中...",
                description="我正在理解你的請求並調度團隊，稍等片刻 ⏳",
                color=0x7289DA,
            )
        )

        # LLM 分析需求
        decision = await leader_analyze(raw_content)
        action = decision.get("action", "dispatch")
        agents_to_dispatch = decision.get("agents", list(TEAM_AGENTS.keys()))
        dispatch_task = decision.get("task", raw_content)
        direct_answer = decision.get("direct_answer", "")
        symbol = decision.get("symbol")

        # 過濾：只調度真實存在的 Agent
        agents_to_dispatch = [k for k in agents_to_dispatch if k in TEAM_AGENTS]
        if not agents_to_dispatch:
            agents_to_dispatch = list(TEAM_AGENTS.keys())

        task_id = f"task_{int(time.time() * 1000)}"

        # 如果是純粹直接回答（不需要團隊）
        if action == "answer" and direct_answer:
            await thinking_msg.edit(
                embed=make_embed(
                    title="💬 回答",
                    description=direct_answer,
                    color=0x00C851,
                    footer=f"任務ID：{task_id}",
                )
            )
            return

        # 建立追蹤狀態訊息
        task = TaskState(task_id, message.channel, thinking_msg)
        task.dispatch_agents = agents_to_dispatch
        task.dispatch_task = dispatch_task

        # 按階段分組：Phase 1 信息收集 → Phase 2 決策分析
        phase1 = [k for k in agents_to_dispatch if AGENT_PHASES.get(k, 2) == 1]
        phase2 = [k for k in agents_to_dispatch if AGENT_PHASES.get(k, 2) == 2]
        task.phase1_agents = phase1
        task.phase2_agents = phase2

        # 初始化所有相關 Agent 狀態
        for key in task.agent_states:
            task.agent_states[key] = STATE_PENDING

        # 先調度 Phase 1（如有），Phase 2 等 Phase 1 完成後再調度
        if phase1:
            first_dispatch = phase1
        else:
            first_dispatch = phase2

        for key in first_dispatch:
            task.agent_states[key] = STATE_SENT

        pending_tasks[task_id] = task
        await update_user_status(task)

        team_ch = client.get_channel(team_channel_id)
        if not team_ch:
            await thinking_msg.edit(
                embed=make_embed(
                    title="⚠️ 錯誤",
                    description="無法訪問團隊頻道，請檢查 TEAM_CHANNEL_ID 配置。",
                    color=0xFF4444,
                )
            )
            return

        # 發任務到團隊頻道（僅發送第一階段的參與成員）
        task_content, task_embed = build_task_message(task_id, dispatch_task, first_dispatch)
        await team_ch.send(content=task_content, embed=task_embed)

        # 如果有 direct_answer，先顯示給用戶
        if direct_answer and action == "hybrid":
            await message.channel.send(
                embed=make_embed(
                    title="💬 先說結論",
                    description=direct_answer,
                    color=0x00C851,
                )
            )

        # 啟動超時監控（含兩階段調度邏輯）
        asyncio.create_task(monitor_task(task, client))

    async def _wait_for_agents(task, agent_keys):
        """等待指定 Agent 完成（或超時）。

        Args:
            task: TaskState 物件，用於讀取/更新 agent_states 和超時設定。
            agent_keys: 要等待的 Agent key 列表。

        每 5 秒輪詢一次，若超過 receive_timeout 仍為 STATE_SENT 則標記為
        STATE_TIMEOUT；所有 agent_keys 進入終態後返回。
        """
        start = time.time()
        receive_deadline = start + task.receive_timeout
        process_deadline = start + task.receive_timeout + task.process_timeout

        while time.time() < process_deadline:
            await asyncio.sleep(5)

            if time.time() > receive_deadline:
                for key in agent_keys:
                    if task.agent_states[key] == STATE_SENT:
                        task.agent_states[key] = STATE_TIMEOUT

            all_done = all(
                task.agent_states[key] in (STATE_DONE, STATE_TIMEOUT, STATE_ERROR)
                for key in agent_keys
            )
            if all_done:
                break

    async def monitor_task(task, client):
        try:
            team_ch = client.get_channel(team_channel_id)

            # ── Phase 1：信息收集層 ──
            if task.phase1_agents:
                await _wait_for_agents(task, task.phase1_agents)

                # Phase 1 完成 → 調度 Phase 2（帶上 Phase 1 報告作為參考）
                if task.phase2_agents and team_ch:
                    phase1_reports = [
                        (name, r) for name, r in task.reports
                        if AGENT_NAME_TO_KEY.get(name) in task.phase1_agents
                    ]
                    for key in task.phase2_agents:
                        task.agent_states[key] = STATE_SENT
                    await update_user_status(task)

                    tc, te = build_task_message(
                        task.task_id, task.dispatch_task, task.phase2_agents,
                        cross_agent_reports=phase1_reports,
                    )
                    await team_ch.send(content=tc, embed=te)

            # ── Phase 2：決策分析層 ──
            if task.phase2_agents:
                await _wait_for_agents(task, task.phase2_agents)

            # ── 匯總 ──
            if task.task_id in pending_tasks:
                pending_tasks.pop(task.task_id)
                await summarize_and_reply(task)
        except Exception as e:
            log.error(f"❌ monitor_task 異常：{e}")

    async def summarize_and_reply(task):
        reports = task.reports
        MSG_MAX = 2000  # Discord 訊息上限

        # 最終狀態：未完成的 Agent 標記為超時
        for key in task.dispatch_agents:
            if task.agent_states[key] != STATE_DONE:
                task.agent_states[key] = STATE_TIMEOUT
        try:
            await update_user_status(task)
        except Exception:
            pass
        await asyncio.sleep(1)

        # LLM 彙總結論
        conclusion = ""
        if reports:
            summary_prompt = f"用戶請求：{task.dispatch_task}\n\n以下是各分析師報告：\n" + "\n\n".join(
                [f"【{name}】：{r}" for name, r in reports]
            ) + "\n\n請用一段話總結結論（50字內），明確给出多/空傾向。"
            conclusion = await call_minimax(
                "你是一個專業的投資總結分析師，請簡潔有力地總結結論。",
                summary_prompt,
                max_tokens=128,
            )

        # 建構最終報告 — 動態計算 field 預算，確保不超過 Discord 6000 字元上限
        EMBED_TOTAL_MAX = 6000
        EMBED_STRUCTURE_OVERHEAD = 50       # JSON 結構與時間戳等額外開銷
        FIELD_NAME_ESTIMATE = 40            # emoji + agent name + state ≈ 40 字元

        title_text = f"🎯 分析報告（{len(reports)}/{len(task.dispatch_agents)} 位成員回覆）"
        desc_text = (
            f"📌 任務：{task.dispatch_task[:200]}\n\n"
            f"⏱ 耗時：{int(time.time() - task.created_at)}秒\n\n"
            + (f"📝 結論：{conclusion}\n\n" if conclusion else "")
        )
        footer_text = "Market Monitor Agent Team"

        # 計算 field 可用預算
        overhead = len(title_text) + len(desc_text) + len(footer_text) + EMBED_STRUCTURE_OVERHEAD
        n_agents = max(len(task.dispatch_agents), 1)
        per_field_val_max = min(1024, max(100, (EMBED_TOTAL_MAX - overhead - n_agents * FIELD_NAME_ESTIMATE) // n_agents))

        fields = []
        for key in task.dispatch_agents:
            ag = TEAM_AGENTS[key]
            state = task.agent_states.get(key, STATE_TIMEOUT)
            report_text = next((r for n, r in reports if n == ag["name"]), "（無報告）")
            if len(report_text) <= 10:
                value = state
            elif len(report_text) > per_field_val_max:
                value = report_text[:per_field_val_max - 1] + "…"
            else:
                value = report_text
            fields.append({
                "name": f"{ag['emoji']} {ag['name']} {state}",
                "value": value,
                "inline": False,
            })

        embed = make_embed(
            title=title_text,
            description=desc_text,
            color=0x00C851,
            footer=footer_text,
            fields=fields,
        )

        # ── 發送最終報告，逐層降級：edit(重試) → send embed → send 純文字 ──
        sent = False

        # 嘗試 1：編輯原始狀態訊息（含一次重試，應對瞬態 500）
        for attempt in range(2):
            try:
                await task.status_msg.edit(embed=embed)
                sent = True
                break
            except Exception as e:
                log.warning(f"⚠️ edit 最終報告失敗（第{attempt+1}次）：{e}")
                if attempt == 0:
                    await asyncio.sleep(2)

        # 嘗試 2：重新發送 embed 到用戶頻道
        if not sent:
            try:
                await task.user_channel.send(embed=embed)
                sent = True
            except Exception as e:
                log.warning(f"⚠️ send embed 失敗：{e}")

        # 嘗試 3：回退為純文字分段發送
        if not sent:
            try:
                fallback = f"**{title_text}**\n{desc_text}"
                for f in fields:
                    fallback += f"\n**{f['name']}**\n{f['value'][:500]}\n"
                for chunk_start in range(0, len(fallback), MSG_MAX):
                    await task.user_channel.send(fallback[chunk_start:chunk_start + MSG_MAX])
                sent = True
            except Exception as e:
                log.error(f"❌ 純文字回退也失敗：{e}")

        if not sent:
            log.error(f"❌ 任務 {task.task_id} 最終報告無法發送")

    # 斜線命令
    @tree.command(name="幫助", description="顯示使用說明")
    async def cmd_help(interaction: discord.Interaction):
        embed = make_embed(
            title="📖 Agent Team 使用說明",
            description="向我發送任何投資相關問題，我會調度專業團隊處理。",
            fields=[
                {"name": "💬 提问方式", "value": "`@LeaderBot 你覺得現在納指怎麼樣？`\n或直接 DM 我", "inline": False},
                {"name": "📊 團隊成員", "value": "📊交易員 📈行業研究員 🌍宏觀策略師\n📰情報官 ⚠️風控官 🔢量化策略師", "inline": False},
                {"name": "🔄 狀態說明", "value": "📤發送中→✅已接收→🔄處理中→✅完成\n❌ 超時則該成員無回覆", "inline": False},
                {"name": "💡 示例外語", "value": "「分析一下蘋果的技術面」\n「比特幣現在風險大嗎？」\n「宏觀角度看美股後市」", "inline": False},
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @tree.command(name="團隊", description="查看團隊成員")
    async def cmd_team(interaction: discord.Interaction):
        fields = [
            {"name": f"{ag['emoji']} {ag['name']}", "value": ag["focus"], "inline": False}
            for ag in TEAM_AGENTS.values()
        ]
        embed = make_embed(
            title="🤖 Agent Team 成員",
            description="共 6 位專業分析師",
            color=0x00C851,
            fields=fields,
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @tree.command(name="ask", description="向團隊提問，AI 會調度最合適的分析師")
    @app_commands.describe(question="你的投資相關問題")
    async def cmd_ask(interaction: discord.Interaction, question: str):
        """主力命令：接收任意投資問題，調用 LLM 調度團隊處理"""
        await interaction.response.defer(thinking=True)

        decision = await leader_analyze(question)
        action = decision.get("action", "dispatch")
        agents_to_dispatch = [k for k in decision.get("agents", []) if k in TEAM_AGENTS]
        if not agents_to_dispatch:
            agents_to_dispatch = list(TEAM_AGENTS.keys())
        dispatch_task = decision.get("task", question)
        direct_answer = decision.get("direct_answer", "")

        task_id = f"task_{int(time.time() * 1000)}"

        # 直接回答
        if action == "answer" and direct_answer:
            embed = make_embed(
                title="💬 回答",
                description=direct_answer,
                color=0x00C851,
                footer=f"任務ID：{task_id}",
            )
            await interaction.followup.send(embed=embed)
            return

        # 建立狀態訊息（根據階段顯示初始狀態）
        phase1 = [k for k in agents_to_dispatch if AGENT_PHASES.get(k, 2) == 1]
        phase2 = [k for k in agents_to_dispatch if AGENT_PHASES.get(k, 2) == 2]
        first_dispatch = phase1 if phase1 else phase2

        fields = []
        for key in agents_to_dispatch:
            ag = TEAM_AGENTS[key]
            state = STATE_SENT if key in first_dispatch else STATE_PENDING
            fields.append({"name": f"{ag['emoji']} {ag['name']}", "value": state, "inline": True})

        status_embed = make_embed(
            title=f"📋 分析任務：{task_id[:15]}...",
            description=f"📌 {dispatch_task[:80]}\n\n⏳ 正在等待團隊回覆...",
            color=0xFFD700,
            footer=f"任務ID：{task_id}",
            fields=fields,
        )
        status_msg = await interaction.followup.send(embed=status_embed)

        # hybrid：先展示結論
        if action == "hybrid" and direct_answer:
            await interaction.followup.send(
                embed=make_embed(title="💬 先說結論", description=direct_answer, color=0x00C851)
            )

        # 初始化任務
        task = TaskState(task_id, interaction.channel, status_msg)
        task.dispatch_agents = agents_to_dispatch
        task.dispatch_task = dispatch_task

        # 按階段分組
        phase1 = [k for k in agents_to_dispatch if AGENT_PHASES.get(k, 2) == 1]
        phase2 = [k for k in agents_to_dispatch if AGENT_PHASES.get(k, 2) == 2]
        task.phase1_agents = phase1
        task.phase2_agents = phase2

        for key in task.agent_states:
            task.agent_states[key] = STATE_PENDING

        # 先調度 Phase 1（如有），否則直接調度 Phase 2
        first_dispatch = phase1 if phase1 else phase2
        for key in first_dispatch:
            task.agent_states[key] = STATE_SENT

        pending_tasks[task_id] = task
        await update_user_status(task)

        team_ch = client.get_channel(team_channel_id)
        if not team_ch:
            await status_msg.edit(
                embed=make_embed(
                    title="⚠️ 錯誤",
                    description="無法訪問團隊頻道，請檢查 TEAM_CHANNEL_ID 配置。",
                    color=0xFF4444,
                )
            )
            return

        task_content, task_embed = build_task_message(task_id, dispatch_task, first_dispatch)
        await team_ch.send(content=task_content, embed=task_embed)
        asyncio.create_task(monitor_task(task, client))

    log.info("🚀 啟動 Leader Bot")
    client.run(bot_token, log_handler=None)


# ══════════════════════════════════════════════════════════════
# Team Agent Bot
# ══════════════════════════════════════════════════════════════

def run_team_agent_bot(bot_token: str, agent_key: str, team_channel_id: int):
    """運行 Team Agent Bot"""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guild_messages = True

    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    agent = TEAM_AGENTS[agent_key]

    def parse_task_message(content: str):
        """解析任務格式（更寬鬆的匹配）"""
        # 匹配 task_id（task_數字）和任務描述
        match = re.search(r"任務ID：`?(\S+)`?", content)
        task_id = match.group(1) if match else None
        # 提取任務描述（在 團隊任務 / 任務 之後的內容）
        desc_match = re.search(r"(?:團隊)?任務[\s：:]*\n?(.+?)(?=\n\n參與成員|$)", content, re.DOTALL)
        task_desc = desc_match.group(1).strip() if desc_match else None
        return task_id, task_desc

    @client.event
    async def on_ready():
        log.info(f"✅ {agent['name']} Bot 上線：{client.user}")
        for guild in client.guilds:
            try:
                await tree.sync(guild=guild)
                log.info(f"✅ {agent['name']} 已同步命令至 {guild.name}")
            except Exception as e:
                log.error(f"❌ {agent['name']} 同步至 {guild.name} 失敗：{e}")
        try:
            await tree.sync()
        except Exception as e:
            log.error(f"❌ {agent['name']} 全局命令同步失敗：{e}")
        team_ch = client.get_channel(team_channel_id)
        if team_ch:
            embed = make_embed(
                title=f"✅ {agent['name']} 已上線",
                description=f"職責：{agent['focus']}",
                color=0x00C851,
            )
            await team_ch.send(embed=embed)

    @client.event
    async def on_message(message: discord.Message):
        if message.author.id == client.user.id:
            return
        if message.channel.id != team_channel_id:
            return

        # 優先從 message.content 解析（Leader 正常發送 content + embed）；
        # 若 content 為空則回退到 embed（相容舊版 Leader 只發 embed 的情況）
        content_to_parse = message.content
        if not content_to_parse and message.embeds:
            embed = message.embeds[0]
            parts = []
            if embed.footer and embed.footer.text:
                parts.append(embed.footer.text)
            if embed.description:
                parts.append(embed.description)
            content_to_parse = "\n".join(parts)

        task_id, task_desc = parse_task_message(content_to_parse)
        if not task_id:
            return

        # 檢查本 Agent 是否在參與成員列表中（兩階段調度時只回應自己被指派的階段）
        participants_match = re.search(r"參與成員：(.+?)(?:\n|$)", content_to_parse or "")
        if participants_match:
            if agent['name'] not in participants_match.group(1):
                return

        log.info(f"📋 {agent['name']} 收到任務：{task_id} — {task_desc[:50] if task_desc else ''}")

        # 回傳已接收確認
        await message.channel.send(
            f"[{agent['name']}] {task_id} ✅ {ACK_MARKER}，開始分析..."
        )

        # 嘗試從任務描述中提取標的
        symbol_match = re.search(r'\b([A-Z]{2,5}(?:-USD)?)\b', task_desc or "")
        symbol = symbol_match.group(1) if symbol_match else None

        # 根據 Agent 角色主動抓取相關數據
        log.info(f"🔍 {agent['name']} 正在為 {symbol or '(無標的)'} 抓取數據...")
        context_data = gather_agent_context(agent_key, symbol)

        # 構造分析消息
        if context_data:
            user_msg = f"""{task_desc}

以下是我為你抓取的即時市場數據：

{context_data}

請基於以上數據和你的專業知識，直接給出你的分析結論。不要索要額外數據，不要說「建議查看」，直接給出明確判斷。"""
        else:
            user_msg = f"""{task_desc}

目前無法獲取該標的的即時數據，但你是專業分析師，請基於你的專業知識和經驗，直接給出你的分析和判斷。
不要索要任何數據，不要推諉，不要說「需要更多信息」，直接給出你能提供的最佳分析。"""

        async with message.channel.typing():
            report = await call_minimax(agent["system_prompt"], user_msg)

        report_msg = f"[{agent['name']}] {task_id} {report}"
        await message.channel.send(report_msg)
        log.info(f"✅ {agent['name']} 已回傳報告（{task_id}）")
        await message.add_reaction("✅")

    @tree.command(name="幫助", description=f"顯示 {agent['name']} 說明")
    async def cmd_help(interaction: discord.Interaction):
        embed = make_embed(
            title=f"🤖 {agent['name']}",
            description=f"職責：{agent['focus']}",
            color=0x7289DA,
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @tree.command(name="測試", description="測試分析功能")
    @app_commands.describe(symbol="股票代碼")
    async def cmd_test(interaction: discord.Interaction, symbol: str):
        await interaction.response.defer()
        context_data = gather_agent_context(agent_key, symbol.upper())
        if not context_data:
            await interaction.followup.send(f"⚠️ 無法取得 {symbol} 任何數據")
            return

        user_msg = f"請分析 {symbol}。\n\n{context_data}"
        report = await call_minimax(agent["system_prompt"], user_msg)

        embed = make_embed(
            title=f"📊 [{symbol}] {agent['name']} 分析",
            description=report,
            color=0x00C851,
        )
        await interaction.followup.send(embed=embed)

    log.info(f"🚀 啟動 {agent['name']} Bot")
    client.run(bot_token, log_handler=None)


# ══════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════

def main():
    """讀取配置，啟動所有 Bot"""
    from src.config import load_config, load_agents_config

    raw_cfg = load_config()
    agents_cfg = load_agents_config(raw_cfg)

    leader_token = os.environ.get("LEADER_BOT_TOKEN")
    team_channel_id = int(os.environ.get("TEAM_CHANNEL_ID", 0))

    if not leader_token:
        log.error("❌ 缺少 LEADER_BOT_TOKEN")
        sys.exit(1)
    if not team_channel_id:
        log.error("❌ 缺少 TEAM_CHANNEL_ID")
        sys.exit(1)

    leader_thread = threading.Thread(
        target=run_leader_bot,
        args=(leader_token, team_channel_id),
        name="LeaderBot",
        daemon=True,
    )
    leader_thread.start()
    log.info("📦 Leader Bot 已啟動")

    agent_threads = []
    for agent_key, cfg in agents_cfg.items():
        if agent_key == "chief_strategist":
            continue

        token_env = cfg.token_env or f"{agent_key.upper()}_TOKEN"
        token = os.environ.get(token_env)
        if not token:
            log.warning(f"⚠️ 跳過 {agent_key}：缺少 {token_env}")
            continue

        t = threading.Thread(
            target=run_team_agent_bot,
            args=(token, agent_key, team_channel_id),
            name=f"{agent_key}Bot",
            daemon=True,
        )
        t.start()
        agent_threads.append(t)
        log.info(f"📦 {agent_key} Bot 已啟動")

    log.info(f"🚀 全部啟動完成，共 {len(agent_threads) + 1} 個 Bot")
    leader_thread.join()


if __name__ == "__main__":
    main()
