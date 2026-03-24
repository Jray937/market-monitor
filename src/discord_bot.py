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

    # 每小時摘要（每4輪發一次）
    if int(time.time()) % 3600 < monitor_interval * 60:
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


async def send_summary_to_channel(channel, results: list):
    if not results:
        return
    bullish, overbought, bearish, neutral = [], [], [], []
    for r in results:
        sym = r["symbol"]
        ta = r["ta"]
        if ta.above_ma200 and ta.current_price > (ta.sma50 or float("inf")):
            bullish.append(sym)
        elif ta.rsi14 and ta.rsi14 > 70:
            overbought.append(sym)
        elif ta.below_ma200 and ta.current_price < (ta.sma50 or 0):
            bearish.append(sym)
        else:
            neutral.append(sym)

    fields = []
    if bullish:   fields.append({"name": "🟢 多頭訊號",   "value": ", ".join(bullish),  "inline": True})
    if overbought:fields.append({"name": "🔥 超買警告",   "value": ", ".join(overbought),"inline": True})
    if bearish:   fields.append({"name": "🔴 空頭訊號",   "value": ", ".join(bearish),  "inline": True})
    if neutral:   fields.append({"name": "⚪ 中性觀望",   "value": ", ".join(neutral),  "inline": True})

    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    embed = make_embed(
        title=f"📋 市場摘要 · {now[11:]}",
        description=f"共監控 **{len(results)}** 檔資產",
        color=0x7289DA,
        fields=fields,
        footer="Market Monitor 每小時摘要",
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
        ta = r["ta"]
        if ta.above_ma200 and ta.current_price > (ta.sma50 or float("inf")):
            bullish.append(r["symbol"])
        elif ta.rsi14 and ta.rsi14 > 70:
            overbought.append(r["symbol"])
        elif ta.below_ma200 and ta.current_price < (ta.sma50 or 0):
            bearish.append(r["symbol"])
        else:
            neutral.append(r["symbol"])

    fields = []
    if bullish:   fields.append({"name": "🟢 多頭訊號",   "value": ", ".join(bullish),  "inline": True})
    if overbought:fields.append({"name": "🔥 超買警告",   "value": ", ".join(overbought),"inline": True})
    if bearish:   fields.append({"name": "🔴 空頭訊號",   "value": ", ".join(bearish),  "inline": True})
    if neutral:   fields.append({"name": "⚪ 中性觀望",   "value": ", ".join(neutral),  "inline": True})

    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    embed = make_embed(
        title=f"📋 市場摘要 · {now[11:]}",
        description=f"共監控 **{len(results)}** 檔資產",
        color=0x7289DA,
        fields=fields,
    )
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


if __name__ == "__main__":
    main()
