"""
Discord Bot（discord.py）
功能：
  - /狀態  查看所有監控標的當前技術指標
  - /查詢  <SYMBOL>  查單一標的詳細分析
  - /新增  <SYMBOL> <警報類型>  新增監控標的（暫存，重啟需重設）
  - 背景監控  每 N 分鐘自動檢查並發送警報
  - 每小時摘要  在指定頻道自動推送

使用 discord.py + 環境變數，Token 不進 GitHub
"""
import os
import sys
import re
import time
import threading
import datetime
import requests
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ── 本地模組 ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import load_config, parse_config, SymbolConfig, AlertRule
from src.data_fetcher import fetch_ohlcv
from src.analyzer import compute_ta, check_alert
from src.alert_manager import AlertManager

# ── 日誌 ──
from src.logger import setup_logger
log = setup_logger("discord_bot")

# ── 全域狀態 ──
alert_mgr: AlertManager | None = None
symbols_cfg: list[SymbolConfig] = []
monitor_interval: int = 15
_channel_id: int | None = None

# ── Discord Intents ──
intents = discord.Intents.default()
intents.message_content = True   # 讀取命令訊息
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ══════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════

def make_embed(
    title: str,
    description: str = "",
    color: int = 0x7289DA,
    fields: list = None,
    footer: str = None,
    timestamp: bool = True,
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    for f in (fields or []):
        embed.add_field(name=f["name"], value=f["value"], inline=f.get("inline", False))
    if footer:
        embed.set_footer(text=footer)
    if timestamp:
        embed.timestamp = datetime.datetime.utcnow()
    return embed

def fmt_price(price: float) -> str:
    return f"${price:.4g}" if price < 100 else f"${price:.2f}"

def fmt_pct(pct: float) -> str:
    return f"{'📈' if pct >= 0 else '📉'} {pct:+.2f}%"

def ta_summary_fields(ta) -> list:
    fields = []
    if ta.rsi14 is not None:
        rsi_emoji = "🔴" if ta.rsi14 > 70 else "🟢" if ta.rsi14 < 30 else "⚪"
        fields.append({"name": "RSI(14)", "value": f"{rsi_emoji} `{ta.rsi14:.1f}`", "inline": True})
    if ta.macd is not None:
        hist = ta.macd - ta.macd_signal
        emoji = "🟢" if hist > 0 else "🔴"
        fields.append({"name": "MACD", "value": f"{emoji} `{ta.macd:.4f}`", "inline": True})
    if ta.sma200 is not None:
        above = "✅" if ta.current_price > ta.sma200 else "⚠️"
        fields.append({"name": "MA200", "value": f"{above} `{fmt_price(ta.sma200)}`", "inline": True})
    if ta.bb_upper is not None:
        fields.append({"name": "布林帶", "value": f"`{fmt_price(ta.bb_lower)}` ~ `{fmt_price(ta.bb_upper)}`", "inline": False})
    return fields

def color_for_signal(ta) -> int:
    if ta.rsi14 and ta.rsi14 > 70:   return 0xFF8C00  # 超買橙色
    if ta.rsi14 and ta.rsi14 < 30:   return 0x00C851  # 超賣綠（反彈）
    if ta.above_ma200:                return 0x00C851  # 多頭綠
    if ta.below_ma200:                return 0xFF4444  # 空頭紅
    return 0x7289DA  # 默認藍

# ══════════════════════════════════════════════════════
# 背景監控任務
# ══════════════════════════════════════════════════════

@tasks.loop(minutes=15)
async def monitor_job():
    """每15分鐘自動執行的市場監控"""
    await client.wait_until_ready()
    if _channel_id is None:
        log.warning("未設定監控頻道，跳過本輪")
        return
    log.info("=== 自動監控輪次開始 ===")
    channel = client.get_channel(_channel_id)
    if channel is None:
        log.error(f"找不到頻道 {_channel_id}")
        return

    from concurrent.futures import ThreadPoolExecutor
    results = []

    def do_monitor(cfg: SymbolConfig):
        sym = cfg.symbol
        df = fetch_ohlcv(sym, period="6mo", interval="1d")
        if df.empty:
            return None
        ta = compute_ta(sym, df)
        if ta is None:
            return None
        # 檢查警報
        for rule in cfg.alerts:
            if alert_mgr and alert_mgr.is_in_cooldown(sym, rule.type):
                continue
            triggered, msg = check_alert(ta, rule.type, rule.threshold)
            if triggered:
                _send_alert_to_channel(channel, ta, rule)
                alert_mgr.record_trigger(sym, rule.type, rule.cooldown_hours)
        return {"symbol": sym, "ta": ta}

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(do_monitor, cfg) for cfg in symbols_cfg]
        for f in futures:
            r = f.result()
            if r:
                results.append(r)

    # 每4小時摘要
    if int(time.time()) % 14400 < monitor_interval * 60:
        await send_summary_to_channel(channel, results)

    log.info(f"=== 自動監控完成，{len(results)} 檔 ===")


def _send_alert_to_channel(channel, ta, rule):
    color_map = {
        "rsi_oversold":   0x00C851,
        "rsi_overbought": 0xFF8C00,
        "macd_cross_up":  0x00C851,
        "macd_cross_down":0xFF4444,
        "price_cross_ma200":"0x7289DA",
        "bollinger_upper":0x00C851,
        "bollinger_lower":0xFF4444,
    }
    emoji_map = {
        "rsi_oversold":   "📉",
        "rsi_overbought": "📈",
        "macd_cross_up":  "✅",
        "macd_cross_down": "🔴",
        "price_cross_ma200":"🚀",
        "bollinger_upper": "💥",
        "bollinger_lower": "📍",
    }
    name_map = {
        "rsi_oversold":   "RSI 超賣",
        "rsi_overbought": "RSI 超買",
        "macd_cross_up":  "MACD 金叉",
        "macd_cross_down": "MACD 死叉",
        "price_cross_ma200":"MA200 均線交叉",
        "bollinger_upper": "布林上軌突破",
        "bollinger_lower": "布林下軌跌破",
    }
    embed = make_embed(
        title=f"{emoji_map.get(rule.type,'⚠️')} [{ta.symbol}] {name_map.get(rule.type, rule.type)}",
        description=f"💰 **{fmt_price(ta.current_price)}**  ({fmt_pct(ta.pct_change)})",
        color=color_map.get(rule.type, 0xFF6B6B),
        fields=ta_summary_fields(ta),
        footer=f"Market Monitor | {ta.symbol}",
    )
    try:
        from discord import Guild
        coro = channel.send(embed=embed)
        asyncio.run_coroutine_threadsafe(coro, client.loop)
    except Exception as e:
        log.error(f"發送警報失敗: {e}")


def _ta_detail_line(ta) -> str:
    """生成單一資產的詳細技術分析一行文字"""
    parts = []

    # RSI
    if ta.rsi14 is not None:
        rsi_state = "超買" if ta.rsi14 > 70 else "超賣" if ta.rsi14 < 30 else "中性"
        parts.append(f"RSI={ta.rsi14:.1f}({rsi_state})")

    # MACD
    if ta.macd is not None and ta.macd_signal is not None:
        hist = ta.macd - ta.macd_signal
        macd_state = "金叉" if hist > 0 else "死叉"
        parts.append(f"MACD={ta.macd:.3f}({macd_state})")

    # MA200
    if ta.sma200 is not None:
        diff_pct = (ta.current_price - ta.sma200) / ta.sma200 * 100
        above = "▲" if diff_pct > 0 else "▼"
        parts.append(f"MA200={fmt_price(ta.sma200)}({above}{abs(diff_pct):.1f}%)")

    # 布林帶
    if ta.bb_upper is not None and ta.bb_lower is not None:
        bb_pos = (ta.current_price - ta.bb_lower) / (ta.bb_upper - ta.bb_lower) * 100
        bb_state = "上軌" if ta.current_price >= ta.bb_upper else "下軌" if ta.current_price <= ta.bb_lower else f"中{bb_pos:.0f}%"
        parts.append(f"BB={bb_state}")

    return " | ".join(parts) if parts else "數據不足"


def _signal_conclusion(ta) -> str:
    """根據技術指標給出結論"""
    conclusions = []

    # RSI 結論
    if ta.rsi14 is not None:
        if ta.rsi14 > 80:
            conclusions.append("RSI 極度超買，回調風險高")
        elif ta.rsi14 > 70:
            conclusions.append("RSI 超買區域，警惕獲利了結")
        elif ta.rsi14 < 20:
            conclusions.append("RSI 極度超賣，強反彈可能")
        elif ta.rsi14 < 30:
            conclusions.append("RSI 超賣區域，留意反彈機會")
        else:
            conclusions.append(f"RSI 中性({ta.rsi14:.1f})，無明顯方向")

    # MA200 結論
    if ta.sma200 is not None:
        if ta.current_price > ta.sma200:
            pct = (ta.current_price - ta.sma200) / ta.sma200 * 100
            conclusions.append(f"多頭格局，價格高於MA200 {pct:.1f}%")
        else:
            pct = (ta.sma200 - ta.current_price) / ta.sma200 * 100
            conclusions.append(f"空頭格局，價格低於MA200 {pct:.1f}%")

    # MACD 結論
    if ta.macd is not None and ta.macd_signal is not None:
        hist = ta.macd - ta.macd_signal
        if hist > 0 and abs(hist) > 0.1:
            conclusions.append("MACD 開口向上，多方動能強")
        elif hist < 0 and abs(hist) > 0.1:
            conclusions.append("MACD 開口向下，空方動能強")
        elif abs(hist) <= 0.05:
            conclusions.append("MACD 交叉附近，方向待確認")

    # 布林帶結論
    if ta.bb_upper is not None and ta.bb_lower is not None:
        bandwidth = ta.bb_upper - ta.bb_lower
        if ta.current_price >= ta.bb_upper:
            conclusions.append("價格突破布林上軌，強勢運行關注回調")
        elif ta.current_price <= ta.bb_lower:
            conclusions.append("價格跌破布林下軌，弱勢運行關注支撐")
        elif bandwidth < ta.bb_lower * 0.03:
            conclusions.append("布林帶收窄，突破機會將至")

    return " • ".join(conclusions) if conclusions else "綜合指標無明確信號"


async def send_summary_to_channel(channel, results: list):
    if not results:
        return

    # 分類
    bullish, overbought, bearish, neutral = [], [], [], []
    for r in results:
        sym = r["symbol"]
        ta = r["ta"]
        if ta.above_ma200 and ta.current_price > (ta.sma50 or float("inf")):
            bullish.append(r)
        elif ta.rsi14 and ta.rsi14 > 70:
            overbought.append(r)
        elif ta.below_ma200 and ta.current_price < (ta.sma50 or 0):
            bearish.append(r)
        else:
            neutral.append(r)

    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    embed = make_embed(
        title=f"📋 市場摘要 · {now[11:]}",
        description=f"共監控 **{len(results)}** 檔資產 | 每4小時更新",
        color=0x7289DA,
        fields=[],
        footer="Market Monitor 每4小時摘要",
    )

    # 多頭訊號
    if bullish:
        lines = []
        for r in bullish:
            ta = r["ta"]
            detail = _ta_detail_line(ta)
            conclusion = _signal_conclusion(ta)
            lines.append(f"**{r['symbol']}** {fmt_price(ta.current_price)} {fmt_pct(ta.pct_change)}\n{detail}\n→ {conclusion}")
        embed.add_field(
            name="🟢 多頭訊號",
            value="\n\n".join(lines)[:1024],
            inline=False
        )

    # 超買警告
    if overbought:
        lines = []
        for r in overbought:
            ta = r["ta"]
            detail = _ta_detail_line(ta)
            conclusion = _signal_conclusion(ta)
            lines.append(f"**{r['symbol']}** {fmt_price(ta.current_price)} {fmt_pct(ta.pct_change)}\n{detail}\n→ {conclusion}")
        embed.add_field(
            name="🔥 超買警告",
            value="\n\n".join(lines)[:1024],
            inline=False
        )

    # 空頭訊號
    if bearish:
        lines = []
        for r in bearish:
            ta = r["ta"]
            detail = _ta_detail_line(ta)
            conclusion = _signal_conclusion(ta)
            lines.append(f"**{r['symbol']}** {fmt_price(ta.current_price)} {fmt_pct(ta.pct_change)}\n{detail}\n→ {conclusion}")
        embed.add_field(
            name="🔴 空頭訊號",
            value="\n\n".join(lines)[:1024],
            inline=False
        )

    # 中性觀望
    if neutral:
        lines = []
        for r in neutral:
            ta = r["ta"]
            detail = _ta_detail_line(ta)
            conclusion = _signal_conclusion(ta)
            lines.append(f"**{r['symbol']}** {fmt_price(ta.current_price)} {fmt_pct(ta.pct_change)}\n{detail}\n→ {conclusion}")
        embed.add_field(
            name="⚪ 中性觀望",
            value="\n\n".join(lines)[:1024],
            inline=False
        )

    await channel.send(embed=embed)


# ══════════════════════════════════════════════════════
# Discord Bot 事件
# ══════════════════════════════════════════════════════

@client.event
async def on_ready():
    log.info(f"✅ Discord Bot 上線：{client.user} ({client.user.id})")
    # 同步斜線命令
    await tree.sync()
    log.info("✅ 斜線命令已同步")
    # 啟動背景監控
    monitor_job.start()
    # 發送上線通知
    if _channel_id:
        ch = client.get_channel(_channel_id)
        if ch:
            now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            embed = make_embed(
                title="✅ Market Monitor Bot 上線",
                description=f"開始監控 **{len(symbols_cfg)}** 檔資產\n指令： `/幫助`",
                color=0x7289DA,
                fields=[
                    {"name": "指令", "value": "`/狀態` `/查詢` `/幫助`", "inline": False},
                    {"name": "技術指標", "value": "RSI · MACD · MA200 · 布林帶", "inline": False},
                ],
                footer=f"上線時間：{now}",
            )
            await ch.send(embed=embed)


# ══════════════════════════════════════════════════════
# 斜線命令
# ══════════════════════════════════════════════════════

@tree.command(name="幫助", description="顯示所有可用指令")
async def cmd_help(interaction: discord.Interaction):
    embed = make_embed(
        title="📖 Market Monitor 指令列表",
        color=0x7289DA,
        fields=[
            {"name": "/狀態", "value": "查看所有監控標的的當前技術指標", "inline": False},
            {"name": "/查詢 <代號>", "value": "查詢單一標的詳細分析\n範例：`/查詢 NVDA`", "inline": False},
            {"name": "/新增 <代號> <警報類型>", "value": "新增監控標的（需填警報類型）\n範例：`/新增 TSLA rsi_overbought`", "inline": False},
            {"name": "/移除 <代號>", "value": "移除監控標的", "inline": False},
            {"name": "/摘要", "value": "立即發送市場摘要報告", "inline": False},
        ],
        footer="警報觸發時自動發送通知，無需手動操作",
    )
    await interaction.response.send_message(embed=embed, ephemeral=False)


@tree.command(name="狀態", description="查看所有監控標的的當前技術指標")
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer()
    msg = await interaction.original_response()

    from concurrent.futures import ThreadPoolExecutor
    rows = []

    def fetch_one(cfg: SymbolConfig):
        df = fetch_ohlcv(cfg.symbol, period="6mo", interval="1d")
        if df.empty:
            return None
        ta = compute_ta(cfg.symbol, df)
        return {"symbol": cfg.symbol, "market": cfg.market, "ta": ta}

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(fetch_one, cfg) for cfg in symbols_cfg]
        results = [f.result() for f in futures if f.result()]

    if not results:
        await interaction.followup.send("⚠️ 無法取得任何數據，請檢查網路或 Symbol 是否正確")
        return

    # 分頁：每個 embed 最少 3 欄
    results.sort(key=lambda r: r["symbol"])
    for r in results:
        ta = r["ta"]
        fields = ta_summary_fields(ta)
        if not fields:
            fields = [{"name": "狀態", "value": "數據不足", "inline": False}]
        embed = make_embed(
            title=f"{'📈' if ta.above_ma200 else '📉'} [{r['symbol']}] {fmt_price(ta.current_price)} {fmt_pct(ta.pct_change)}",
            description="",
            color=color_for_signal(ta),
            fields=fields,
            footer=f"{r['market'].upper()} | Market Monitor",
        )
        await msg.reply(embed=embed)

    await interaction.followup.send(f"✅ 已更新 **{len(results)}** 檔狀態")


@tree.command(name="查詢", description="查詢單一標的的詳細技術分析")
@app_commands.describe(symbol="股票或加密貨幣代碼，例如：NVDA、BTC-USD")
async def cmd_query(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer()
    sym = symbol.strip().upper()

    df = fetch_ohlcv(sym, period="6mo", interval="1d")
    if df.empty:
        await interaction.followup.send(f"⚠️ 無法取得 `{sym}` 的數據，請確認代碼正確")
        return

    ta = compute_ta(sym, df)
    if ta is None:
        await interaction.followup.send(f"⚠️ `{sym}` 數據不足，無法分析")
        return

    # 價格 + 漲跌
    price_desc = f"💰 **{fmt_price(ta.current_price)}**  ({fmt_pct(ta.pct_change)})"

    # RSI 描述
    rsi_desc = ""
    if ta.rsi14 is not None:
        if ta.rsi14 > 70:   rsi_desc = "🔥 RSI 超買區域（>70）"
        elif ta.rsi14 < 30: rsi_desc = "🛋️ RSI 超賣區域（<30）"
        else:               rsi_desc = f"RSI 中性區域（{ta.rsi14:.1f}）"

    # MA200 描述
    ma_desc = ""
    if ta.sma200 is not None:
        if ta.current_price > ta.sma200:  ma_desc = f"✅ 價格 ${ta.current_price:.2f} > MA200 ${ta.sma200:.2f}（多頭）"
        else:                              ma_desc = f"⚠️ 價格 ${ta.current_price:.2f} < MA200 ${ta.sma200:.2f}（空頭）"

    embed = make_embed(
        title=f"📊 [{sym}] 技術分析報告",
        description=price_desc,
        color=color_for_signal(ta),
        fields=[
            {"name": "📊 RSI(14)",     "value": f"`{ta.rsi14:.1f}` — {rsi_desc}", "inline": False},
            {"name": "📈 MA200 狀態",   "value": ma_desc,                           "inline": False},
            {"name": "MACD",            "value": f"`{ta.macd:.4f}`  信號線：`{ta.macd_signal:.4f}`", "inline": False},
            {"name": "📐 布林帶",       "value": f"下軌 `${ta.bb_lower:.2f}` 中軌 `${ta.bb_middle:.2f}` 上軌 `${ta.bb_upper:.2f}`", "inline": False},
            {"name": "SMA 均線",        "value": f"SMA20=`${ta.sma20:.2f}` SMA50=`${ta.sma50:.2f}` SMA200=`${ta.sma200:.2f}`", "inline": False},
        ],
        footer=f"Market Monitor | {sym}",
    )
    await interaction.followup.send(embed=embed)


@tree.command(name="摘要", description="立即發送市場摘要報告")
async def cmd_summary(interaction: discord.Interaction):
    await interaction.response.defer()

    from concurrent.futures import ThreadPoolExecutor

    def fetch_one(cfg: SymbolConfig):
        df = fetch_ohlcv(cfg.symbol, period="6mo", interval="1d")
        if df.empty: return None
        ta = compute_ta(cfg.symbol, df)
        return {"symbol": cfg.symbol, "ta": ta} if ta else None

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = [f.result() for f in [ex.submit(fetch_one, cfg) for cfg in symbols_cfg] if f.result()]

    if not results:
        await interaction.followup.send("⚠️ 無法取得數據")
        return

    bullish, overbought, bearish, neutral = [], [], [], []
    for r in results:
        sym = r["symbol"]
        ta = r["ta"]
        if ta.above_ma200 and ta.current_price > (ta.sma50 or float("inf")):
            bullish.append(r)
        elif ta.rsi14 and ta.rsi14 > 70:
            overbought.append(r)
        elif ta.below_ma200 and ta.current_price < (ta.sma50 or 0):
            bearish.append(r)
        else:
            neutral.append(r)

    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    embed = make_embed(
        title=f"📋 市場摘要 · {now[11:]}",
        description=f"共監控 **{len(results)}** 檔資產 | 每4小時更新",
        color=0x7289DA,
        fields=[],
        footer="Market Monitor 每4小時摘要",
    )

    if bullish:
        lines = []
        for r in bullish:
            ta = r["ta"]
            detail = _ta_detail_line(ta)
            conclusion = _signal_conclusion(ta)
            lines.append(f"**{r['symbol']}** {fmt_price(ta.current_price)} {fmt_pct(ta.pct_change)}\n{detail}\n→ {conclusion}")
        embed.add_field(name="🟢 多頭訊號", value="\n\n".join(lines)[:1024], inline=False)

    if overbought:
        lines = []
        for r in overbought:
            ta = r["ta"]
            detail = _ta_detail_line(ta)
            conclusion = _signal_conclusion(ta)
            lines.append(f"**{r['symbol']}** {fmt_price(ta.current_price)} {fmt_pct(ta.pct_change)}\n{detail}\n→ {conclusion}")
        embed.add_field(name="🔥 超買警告", value="\n\n".join(lines)[:1024], inline=False)

    if bearish:
        lines = []
        for r in bearish:
            ta = r["ta"]
            detail = _ta_detail_line(ta)
            conclusion = _signal_conclusion(ta)
            lines.append(f"**{r['symbol']}** {fmt_price(ta.current_price)} {fmt_pct(ta.pct_change)}\n{detail}\n→ {conclusion}")
        embed.add_field(name="🔴 空頭訊號", value="\n\n".join(lines)[:1024], inline=False)

    if neutral:
        lines = []
        for r in neutral:
            ta = r["ta"]
            detail = _ta_detail_line(ta)
            conclusion = _signal_conclusion(ta)
            lines.append(f"**{r['symbol']}** {fmt_price(ta.current_price)} {fmt_pct(ta.pct_change)}\n{detail}\n→ {conclusion}")
        embed.add_field(name="⚪ 中性觀望", value="\n\n".join(lines)[:1024], inline=False)

    await interaction.followup.send(embed=embed)


# ══════════════════════════════════════════════════════
# 主程式（讀取環境變數啟動）
# ══════════════════════════════════════════════════════

def main():
    global alert_mgr, symbols_cfg, monitor_interval, _channel_id

    import asyncio

    # 讀取設定
    raw_cfg = load_config()
    monitor_cfg, symbols_cfg = parse_config(raw_cfg)
    monitor_interval = monitor_cfg.interval_minutes
    alert_mgr = AlertManager()

    # 讀取環境變數
    bot_token = os.environ.get("DISCORD_BOT_TOKEN")
    if not bot_token:
        log.error("❌ 缺少 DISCORD_BOT_TOKEN，請在 Railway 設定環境變數")
        sys.exit(1)

    channel_id_str = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if channel_id_str:
        try:
            _channel_id = int(channel_id_str)
        except ValueError:
            log.warning(f"無效的 DISCORD_CHANNEL_ID：{channel_id_str}，將不使用固定頻道")
    else:
        log.warning("未設定 DISCORD_CHANNEL_ID，背景監控將在 Bot 加入的任何頻道內執行")

    log.info(f"監控 {len(symbols_cfg)} 檔資產，間隔 {monitor_interval} 分鐘")
    log.info("=" * 50)

    # 啟動 Bot
    client.run(bot_token, log_handler=None)


# ═══════════════════════════════════════════════════════════════════
# 期權命令（需 POLYGON_API_KEY）
# ═══════════════════════════════════════════════════════════════════

@app_commands.describe(symbol="股票代碼，例如：AAPL、TSLA")
@tree.command(name="期权到期", description="查看指定股票的期權到期日")
async def cmd_option_expiry(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer()
    sym = symbol.strip().upper()
    try:
        from src.options_fetcher import get_option_expirations
        expirations = get_option_expirations(sym)
    except ValueError as e:
        await interaction.followup.send(f"⚠️ {e}")
        return
    except Exception as e:
        await interaction.followup.send(f"⚠️ 取得期權到期日失敗：{e}")
        return
    if not expirations:
        await interaction.followup.send(f"⚠️ 找不到 {sym} 的期權數據")
        return
    recent = expirations[:6]
    lines = [f"**📅 {sym} 期權到期日**\n"]
    for exp in recent:
        days = exp["days_to_expiry"]
        label = "（本週）" if days <= 7 else ("（下週）" if days <= 14 else "")
        lines.append(f"  `{exp['date']}`  還有 {days} 天 {label}")
    if len(expirations) > 6:
        lines.append(f"\n_還有 {len(expirations) - 6} 個更多到期日_")
    lines.append("\n使用 `/期权链 <代碼> <到期日>` 查看詳細 chain")
    await interaction.followup.send("\n".join(lines))


@app_commands.describe(symbol="股票代碼", expiration="到期日 (YYYY-MM-DD)")
@tree.command(name="期权链", description="查看指定到期日的完整期權鏈")
async def cmd_option_chain(interaction: discord.Interaction, symbol: str, expiration: str):
    await interaction.response.defer()
    sym = symbol.strip().upper()
    try:
        from src.options_fetcher import get_option_chain
        chain = get_option_chain(sym, expiration)
    except ValueError as e:
        await interaction.followup.send(f"⚠️ {e}")
        return
    except Exception as e:
        await interaction.followup.send(f"⚠️ 取得期權鏈失敗：{e}")
        return
    underlying = chain.get("underlying_price", 0)
    calls, puts = chain.get("calls", []), chain.get("puts", [])
    if not calls and not puts:
        await interaction.followup.send(f"⚠️ 找不到 {sym} {expiration} 的期權數據")
        return

    atm_call = min(calls, key=lambda x: abs(x["strike"] - underlying)) if underlying and calls else None
    atm_put = min(puts, key=lambda x: abs(x["strike"] - underlying)) if underlying and puts else None
    fields = [
        {"name": "標的現價", "value": f"**${underlying:.2f}**", "inline": True},
        {"name": "到期日", "value": f"`{expiration}`", "inline": True},
        {"name": "合約數量", "value": f"Calls: {len(calls)} | Puts: {len(puts)}", "inline": False},
    ]
    if atm_call:
        fields.append({"name": "📈 ATM Call", "value": f"行權 `${atm_call['strike']:.2f}` 報價 `${atm_call['last']:.2f}` IV `{atm_call['iv']:.1f}%` OI `{atm_call['oi']:,}` Δ `{atm_call['delta']:.3f}`", "inline": False})
    if atm_put:
        fields.append({"name": "📉 ATM Put", "value": f"行權 `${atm_put['strike']:.2f}` 報價 `${atm_put['last']:.2f}` IV `{atm_put['iv']:.1f}%` OI `{atm_put['oi']:,}` Δ `{atm_put['delta']:.3f}`", "inline": False})
    fields.append({"name": "📊 ITM/OTM", "value": f"C ITM:{sum(1 for c in calls if c['itm']=='ITM')} C OTM:{sum(1 for c in calls if c['itm']=='OTM')} P ITM:{sum(1 for p in puts if p['itm']=='ITM')} P OTM:{sum(1 for p in puts if p['itm']=='OTM')}", "inline": False})
    embed = make_embed(title=f"⛓️ [{sym}] 期權鏈 — {expiration}", description=f"標的現價：**${underlying:.2f}**", color=0x00C851, fields=fields, footer=f"Market Monitor | 免費版 Polygon.io")
    await interaction.followup.send(embed=embed)


@app_commands.describe(symbol="股票代碼", expiration="到期日 (YYYY-MM-DD)")
@tree.command(name="期权墙", description="查看期權牆（OI Wall）— 哪個行權價堆積最多未平倉量")
async def cmd_option_wall(interaction: discord.Interaction, symbol: str, expiration: str):
    await interaction.response.defer()
    sym = symbol.strip().upper()
    try:
        from src.options_fetcher import build_options_wall
        wall = build_options_wall(sym, expiration)
    except ValueError as e:
        await interaction.followup.send(f"⚠️ {e}")
        return
    except Exception as e:
        await interaction.followup.send(f"⚠️ 取得期權牆失敗：{e}")
        return
    underlying = wall.get("underlying_price", 0)
    oi_wall = wall.get("oi_wall")
    calls, puts = wall.get("calls", []), wall.get("puts", [])
    total_calls_oi = wall.get("total_calls_oi", 0)
    total_puts_oi = wall.get("total_puts_oi", 0)
    if not calls and not puts:
        await interaction.followup.send(f"⚠️ 找不到 {sym} {expiration} 的 OI 數據")
        return

    wall_desc = ""
    if oi_wall:
        emoji = "📈" if oi_wall["type"] == "call" else "📉"
        wall_desc = f"{emoji} **OI 牆：${oi_wall['strike']:.2f}** ({oi_wall['type'].upper()}) — OI: {oi_wall['oi']:,} 合約\n"

    top_puts = sorted(puts, key=lambda x: x["oi"], reverse=True)[:5]
    top_calls = sorted(calls, key=lambda x: x["oi"], reverse=True)[:5]
    ratio = total_calls_oi / max(total_puts_oi, 1)
    fields = [
        {"name": "📉 Put OI 排行", "value": "\n".join([f"`${p['strike']:.2f}` {p['itm'][0]}  {p['oi']:>8,}OI" for p in top_puts]) or "無數據", "inline": True},
        {"name": "📈 Call OI 排行", "value": "\n".join([f"`${c['strike']:.2f}` {c['itm'][0]}  {c['oi']:>8,}OI" for c in top_calls]) or "無數據", "inline": True},
        {"name": "📊 總OI 比率", "value": f"C: **{total_calls_oi:,}**  |  P: **{total_puts_oi:,}**  |  C/P = **{ratio:.2f}**", "inline": False},
    ]
    embed = make_embed(title=f"🧱 [{sym}] 期權牆 — {expiration}", description=f"{wall_desc}📍 現價：**${underlying:.2f}**" if underlying else f"{wall_desc}到期日：`{expiration}`", color=0xFF8C00, fields=fields, footer=f"Market Monitor | OI=未平倉量")
    await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    main()
