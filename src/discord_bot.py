"""
Discord 通知發送（使用 Discord Webhook，無需 Bot Token）
比 Telegram 更美觀，支援 Rich Embed
"""
import os
import requests
from .logger import setup_logger

log = setup_logger("discord")

_webhook_url: str = ""

def init_discord(webhook_url: str):
    global _webhook_url
    _webhook_url = webhook_url
    log.info(f"Discord Webhook 初始化完成（url={'***' + webhook_url[-20:]})")

def _send_payload(payload: dict) -> bool:
    if not _webhook_url:
        log.error("Discord Webhook 未設定")
        return False
    try:
        r = requests.post(_webhook_url, json=payload, timeout=10)
        if r.status_code in (200, 204):
            return True
        log.error(f"Discord 請求失敗：{r.status_code} {r.text}")
        return False
    except Exception as e:
        log.error(f"Discord 發送失敗：{e}")
        return False

def send_alert(
    title: str,
    description: str,
    color: int = 0xFF6B6B,
    fields: list = None,
    footer: str = None,
) -> bool:
    """發送一個 Discord Embed 警報"""
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": fields or [],
    }
    if footer:
        embed["footer"] = {"text": footer}
    embed["timestamp"] = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return _send_payload({"embeds": [embed]})

def send_simple_message(text: str) -> bool:
    """發送純文字訊息（極簡模式）"""
    return _send_payload({"content": text})

# ───── 便捷方法 ─────

def send_alert_rsi_oversold(symbol: str, price: float, pct: float, rsi: float, threshold: float) -> bool:
    color_map = {0xFF4444: "超賣", 0x44FF44: "超買"}
    color = 0xFF4444  # 紅色 = 下跌
    return send_alert(
        title=f"📉 [{symbol}] RSI 超賣",
        description=f"💰 **${price:.4g}** ({pct:+.2f}%)\nRSI(14): **{rsi:.1f}** < {threshold}",
        color=color,
        fields=[
            {"name": "信號", "value": "⚠️ 超賣 → 關注反彈機會", "inline": False},
        ],
        footer=f"Market Monitor | {symbol}",
    )

def send_alert_rsi_overbought(symbol: str, price: float, pct: float, rsi: float, threshold: float) -> bool:
    return send_alert(
        title=f"📈 [{symbol}] RSI 超買",
        description=f"💰 **${price:.4g}** ({pct:+.2f}%)\nRSI(14): **{rsi:.1f}** > {threshold}",
        color=0xFF8C00,  # 橙色
        fields=[
            {"name": "信號", "value": "🔥 超買 → 注意獲利了結", "inline": False},
        ],
        footer=f"Market Monitor | {symbol}",
    )

def send_alert_macd_cross_up(symbol: str, price: float, pct: float, macd: float, macd_sig: float, rsi: float) -> bool:
    return send_alert(
        title=f"✅ [{symbol}] MACD 金叉",
        description=f"💰 **${price:.4g}** ({pct:+.2f}%)",
        color=0x00C851,  # 綠色
        fields=[
            {"name": "MACD", "value": f"`{macd:.4f}` > 信號線 `{macd_sig:.4f}`", "inline": True},
            {"name": "RSI(14)", "value": f"`{rsi:.1f}`", "inline": True},
            {"name": "信號", "value": "🟢 多頭動能轉強", "inline": False},
        ],
        footer=f"Market Monitor | {symbol}",
    )

def send_alert_macd_cross_down(symbol: str, price: float, pct: float, macd: float, macd_sig: float, rsi: float) -> bool:
    return send_alert(
        title=f"🔴 [{symbol}] MACD 死叉",
        description=f"💰 **${price:.4g}** ({pct:+.2f}%)",
        color=0xFF4444,  # 紅色
        fields=[
            {"name": "MACD", "value": f"`{macd:.4f}` < 信號線 `{macd_sig:.4f}`", "inline": True},
            {"name": "RSI(14)", "value": f"`{rsi:.1f}`", "inline": True},
            {"name": "信號", "value": "🔴 空頭動能放大", "inline": False},
        ],
        footer=f"Market Monitor | {symbol}",
    )

def send_alert_ma200_cross(symbol: str, price: float, pct: float, ma200: float, direction: str) -> bool:
    color = 0x00C851 if direction == "up" else 0xFF4444
    emoji = "🚀" if direction == "up" else "📉"
    msg = "突破 MA200 → 多頭趨勢" if direction == "up" else "跌破 MA200 → 空頭趨勢"
    return send_alert(
        title=f"{emoji} [{symbol}] MA200 均線{'突破' if direction=='up' else '跌破'}",
        description=f"💰 **${price:.4g}** ({pct:+.2f}%)\nMA200: **${ma200:.2f}**",
        color=color,
        fields=[{"name": "信號", "value": msg, "inline": False}],
        footer=f"Market Monitor | {symbol}",
    )

def send_alert_bollinger(symbol: str, price: float, pct: float, bb_up: float, bb_low: float, direction: str) -> bool:
    color = 0x00C851 if direction == "upper" else 0xFF4444
    emoji = "💥" if direction == "upper" else "📍"
    msg = "觸碰布林上軌 → 強勢突破" if direction == "upper" else "觸碰布林下軌 → 弱勢跌破"
    return send_alert(
        title=f"{emoji} [{symbol}] 布林帶{'上' if direction=='upper' else '下'}軌",
        description=f"💰 **${price:.4g}** ({pct:+.2f}%)\n布林帶：${bb_low:.2f} ~ ${bb_up:.2f}",
        color=color,
        fields=[{"name": "信號", "value": msg, "inline": False}],
        footer=f"Market Monitor | {symbol}",
    )

def send_online_notification(symbol_count: int) -> bool:
    """上線通知"""
    import datetime
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return send_alert(
        title="✅ Market Monitor 上線",
        description=f"開始監控 **{symbol_count}** 檔資產\n間隔：每 15 分鐘一次",
        color=0x7289DA,
        fields=[
            {"name": "資料來源", "value": "Yahoo Finance（免費）", "inline": False},
            {"name": "技術指標", "value": "RSI · MACD · MA200 · 布林帶", "inline": False},
        ],
        footer=f"上線時間：{now_utc}",
    )

def send_summary(results: list) -> bool:
    """每小時摘要"""
    import datetime
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    bullish, overbought, bearish, neutral = [], [], [], []
    for r in results:
        sym = r["symbol"]
        ta = r["ta"]
        if ta.above_ma200 and (ta.sma50 and ta.current_price > ta.sma50):
            bullish.append(sym)
        elif ta.rsi14 and ta.rsi14 > 70:
            overbought.append(sym)
        elif ta.below_ma200 and (ta.sma50 and ta.current_price < ta.sma50):
            bearish.append(sym)
        else:
            neutral.append(sym)

    fields = []
    if bullish:   fields.append({"name": "🟢 多頭訊號",   "value": ", ".join(bullish),  "inline": True})
    if overbought: fields.append({"name": "🔥 超買警告",   "value": ", ".join(overbought),"inline": True})
    if bearish:   fields.append({"name": "🔴 空頭訊號",   "value": ", ".join(bearish),  "inline": True})
    if neutral:   fields.append({"name": "⚪ 中性觀望",   "value": ", ".join(neutral),  "inline": True})

    return send_alert(
        title=f"📋 市場摘要 · {now_utc[11:16]} UTC",
        description=f"共監控 **{len(results)}** 檔資產",
        color=0x7289DA,
        fields=fields,
        footer="Market Monitor 每小時摘要",
    )
