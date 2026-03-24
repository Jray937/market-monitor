"""
Telegram 通知發送
"""
import asyncio
import telegram
from telegram.constants import ParseMode
from .logger import setup_logger

log = setup_logger("telegram")

# 全域 bot 實例（延遲初始化）
_bot: telegram.Bot | None = None
_chat_id: str = ""


def init_bot(bot_token: str, chat_id: str):
    global _bot, _chat_id
    _bot = telegram.Bot(token=bot_token)
    _chat_id = chat_id
    log.info(f"Telegram Bot 初始化完成，chat_id={chat_id}")


async def _send_message(text: str, disable_notification: bool = False) -> bool:
    if not _bot:
        log.error("Bot 未初始化，請先調用 init_bot()")
        return False
    try:
        await _bot.send_message(
            chat_id=_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_notification=disable_notification,
        )
        return True
    except Exception as e:
        log.error(f"Telegram 發送失敗：{e}")
        return False


def send_alert_sync(alert_text: str) -> bool:
    """同步發送警報（供排程線程調用）"""
    try:
        result = asyncio.run(_send_message(alert_text))
        if result:
            log.info(f"✅ 警報已發送")
        return result
    except Exception as e:
        log.error(f"❌ 警報發送失敗：{e}")
        return False


def send_alert(alert_text: str) -> bool:
    """非同步接口，內部啟動新的 event loop"""
    return send_alert_sync(alert_text)


def format_price_change(pct: float) -> str:
    emoji = "📈" if pct >= 0 else "📉"
    return f"{emoji} {pct:+.2f}%"


def build_alert_message(
    symbol: str,
    price: float,
    pct_change: float,
    ta_summary: dict,
    alert_msg: str,
    alert_type: str,
) -> str:
    """組建格式化的警報訊息"""
    emoji_map = {
        "rsi_overbought": "📈",
        "rsi_oversold": "📉",
        "macd_cross_up": "✅",
        "macd_cross_down": "🔴",
        "price_cross_ma200": "🔄",
        "bollinger_upper": "💥",
        "bollinger_lower": "📍",
    }
    emoji = emoji_map.get(alert_type, "⚠️")
    direction_map = {
        "rsi_overbought": "超買警告",
        "rsi_oversold": "超賣警告",
        "macd_cross_up": "多頭金叉",
        "macd_cross_down": "空頭死叉",
        "price_cross_ma200": "MA200 均線交叉",
        "bollinger_upper": "布林上軌突破",
        "bollinger_lower": "布林下軌跌破",
    }
    direction = direction_map.get(alert_type, alert_type)

    # RSI / MACD 狀態
    rsi_val = ta_summary.get("rsi14")
    macd_val = ta_summary.get("macd")
    macd_sig = ta_summary.get("macd_signal")
    bb_up = ta_summary.get("bb_upper")
    bb_low = ta_summary.get("bb_lower")
    ma200 = ta_summary.get("sma200")

    import datetime
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"{emoji} <b>[{symbol}] {direction}</b>",
        f"",
        f"💰 價格：${price:.4g}  ({pct_change:+.2f}%)",
    ]

    if rsi_val is not None:
        rsi_emoji = "🔥" if rsi_val > 70 else "🛋️" if rsi_val < 30 else ""
        lines.append(f"📊 RSI(14)：{rsi_val:.1f} {rsi_emoji}")

    if macd_val is not None and macd_sig is not None:
        hist = macd_val - macd_sig
        hist_emoji = "🟢" if hist > 0 else "🔴"
        lines.append(f"{hist_emoji} MACD：{macd_val:.4f} / 信號線：{macd_sig:.4f}")

    if bb_up is not None and bb_low is not None:
        lines.append(f"📐 布林帶：${bb_low:.2f} ~ ${bb_up:.2f}")

    if ma200 is not None:
        above = "✅" if price > ma200 else "⚠️"
        lines.append(f"{above} MA200：${ma200:.2f}")

    lines.append(f"")
    lines.append(f"{alert_msg}")
    lines.append(f"")
    lines.append(f"🕐 {now}")

    return "\n".join(lines)


def build_summary_message(results: list, utc_time: str) -> str:
    """組建每小時摘要訊息"""
    lines = [
        f"📋 <b>市場摘要</b> · {utc_time}",
        f"",
    ]

    categories = {
        "bullish": [],
        "overbought": [],
        "bearish": [],
        "neutral": [],
    }

    for r in results:
        sym = r["symbol"]
        ta = r["ta"]
        price = ta.current_price
        pct = ta.pct_change
        rsi = ta.rsi14

        if ta.above_ma200 and ta.current_price > ta.sma50:
            categories["bullish"].append(sym)
        elif rsi and rsi > 70:
            categories["overbought"].append(sym)
        elif ta.below_ma200 and ta.current_price < ta.sma50:
            categories["bearish"].append(sym)
        else:
            categories["neutral"].append(sym)

    if categories["bullish"]:
        lines.append(f"🟢 多頭：{', '.join(categories['bullish'])}")
    if categories["overbought"]:
        lines.append(f"🔥 超買：{', '.join(categories['overbought'])}")
    if categories["bearish"]:
        lines.append(f"🔴 空頭：{', '.join(categories['bearish'])}")
    if categories["neutral"]:
        lines.append(f"⚪ 中性：{', '.join(categories['neutral'])}")

    lines.append(f"")
    lines.append(f"共監控 {len(results)} 檔資産")

    return "\n".join(lines)
