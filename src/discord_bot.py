"""
Discord Bot — 多 Bot 協調架構
功能：
  - Railway 1 個服務
  - 7 個 Discord Bot（各自分散式運行）
  - Leader Bot 接收需求，發任務到團隊頻道
  - 各 Agent Bot 監聽團隊頻道，分析並回傳
  - Leader Bot 彙總回覆給用戶

狀態流程：
  發送中 → 已接收（或 接收超時）→ 處理中 → 匯總中 → 完成
"""
import os
import sys
import re
import time
import asyncio
import threading
import datetime
import discord
from discord import app_commands
from typing import Optional

# ── 本地模組 ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.data_fetcher import fetch_ohlcv
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
        "focus": "技術分析、進出场點位",
        "system_prompt": """你是一位專業的交易員，專注於短線和波段交易。
當收到分析任務時，請根據技術指標數據給出：
1. 趨勢判斷（多頭/空頭/震盪）
2. 關鍵支撐阻力位
3. 進場/止損/目標價建議
4. 持有時間框架
保持簡潔，專業，給出具體數字。""",
    },
    "sector_analyst": {
        "name": "行業研究員",
        "emoji": "📈",
        "focus": "基本面、行業、估值",
        "system_prompt": """你是一位資深的行業研究分析師。
當收到分析任務時，請根據技術指標數據給出：
1. 基本面簡評
2. 估值判斷（貴/合理/便宜）
3. 投資評級（買入/持有/減持）
4. 主要風險
保持簡潔，專業，有數據支撐。""",
    },
    "macro_strategist": {
        "name": "宏觀策略師",
        "emoji": "🌍",
        "focus": "宏觀經濟、政策影響",
        "system_prompt": """你是一位頂級的宏觀經濟策略分析師。
當收到分析任務時，請給出：
1. 宏觀背景分析
2. 板塊順風/逆風因素
3. 利率、通脹、政策影響
4. 主要風險情景
保持簡潔，有大局觀。""",
    },
    "intelligence_officer": {
        "name": "情報官",
        "emoji": "📰",
        "focus": "新聞、情緒、消息面",
        "system_prompt": """你是一位敏銳的市場情報分析師。
當收到分析任務時，請根據技術指標給出：
1. 近期重要新聞觀察
2. 市場情緒判斷
3. 信息面風險和機會
保持簡潔，敏銳，注重事實。""",
    },
    "risk_officer": {
        "name": "風控官",
        "emoji": "⚠️",
        "focus": "風險評估、倉位建議",
        "system_prompt": """你是一位嚴格的風險控制專家。
當收到分析任務時，請根據技術指標給出：
1. 波動率風險評估
2. 下行空間分析
3. 合理倉位建議
4. 風險預警
保持簡潔，直接，注重風險。""",
    },
    "quant_strategist": {
        "name": "量化策略師",
        "emoji": "🔢",
        "focus": "量化信號、統計分析",
        "system_prompt": """你是一位量化投資策略師。
當收到分析任務時，請根據技術指標給出：
1. 量化視角分析
2. 統計規律識別
3. 動量和趨勢強度
4. 量化信號評分
保持簡潔，數據驅動。""",
    },
}

# Agent key → display name mapping for report parsing
AGENT_NAME_TO_KEY = {ag["name"]: key for key, ag in TEAM_AGENTS.items()}

# ══════════════════════════════════════════════════════════════
# 狀態定義
# ══════════════════════════════════════════════════════════════

STATE_PENDING   = "⏳ 等待中"
STATE_SENT      = "📤 發送中"
STATE_RECEIVED  = "✅ 已接收"
STATE_TIMEOUT   = "❌ 接收超時"
STATE_PROCESSING= "🔄 處理中"
STATE_SUMMARIZING = "📝 匯總中"
STATE_DONE      = "✅ 完成"
STATE_ERROR     = "⚠️ 錯誤"

AGENT_STATES = [
    STATE_PENDING,
    STATE_SENT,
    STATE_RECEIVED,
    STATE_TIMEOUT,
    STATE_PROCESSING,
    STATE_SUMMARIZING,
    STATE_DONE,
    STATE_ERROR,
]


# ══════════════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════════════

def make_embed(title, description="", color=0x7289DA, fields=None, footer=None):
    embed = discord.Embed(title=title, description=description, color=color)
    for f in (fields or []):
        embed.add_field(name=f["name"], value=f["value"], inline=f.get("inline", False))
    if footer:
        embed.set_footer(text=footer)
    embed.timestamp = datetime.datetime.utcnow()
    return embed

def fmt_price(price: float) -> str:
    return f"${price:.4g}" if price < 100 else f"${price:.2f}"

def fmt_pct(pct: float) -> str:
    return f"{'📈' if pct >= 0 else '📉'} {pct:+.2f}%"


# ══════════════════════════════════════════════════════════════
# AI 調用
# ══════════════════════════════════════════════════════════════

async def call_minimax(system_prompt: str, user_message: str) -> str:
    """調用 MiniMax API"""
    import anthropic

    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        return "⚠️ 未設定 MINIMAX_API_KEY"

    base_url = os.environ.get("MINIMAX_API_BASE_URL", "https://api.minimaxi.com/anthropic")
    model = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

    try:
        client = anthropic.Anthropic(
            api_key=api_key,
            base_url=base_url,
            http_proxy=proxy if proxy else None,
            https_proxy=proxy if proxy else None,
        )

        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        return response.content[0].text if response.content else "⚠️ 無回應"

    except Exception as e:
        log.error(f"❌ MiniMax API 錯誤：{e}")
        return f"⚠️ 分析失敗"


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


# ══════════════════════════════════════════════════════════════
# Leader Bot（接收需求，分發任務，蒐集回覆）
# ══════════════════════════════════════════════════════════════

def run_leader_bot(bot_token: str, team_channel_id: int, user_channel_id: int = None):
    """運行 Leader Bot"""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True
    intents.guild_messages = True

    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    # task_id -> TaskState
    pending_tasks: dict = {}

    class TaskState:
        """單一任務的完整狀態追蹤"""
        def __init__(self, symbol: str, task_id: str, user_channel, status_msg):
            self.symbol = symbol
            self.task_id = task_id
            self.user_channel = user_channel
            self.status_msg = status_msg
            # 每個 Agent 的狀態：key → state string
            self.agent_states: dict = {key: STATE_PENDING for key in TEAM_AGENTS}
            self.reports: list = []  # (agent_name, report_text)
            self.created_at = time.time()
            self.receive_timeout = 120  # 秒，接收超時
            self.process_timeout = 180   # 秒，處理超時

    def build_status_embed(task: TaskState) -> discord.Embed:
        """根據當前任務狀態建構 Embed"""
        fields = []
        for key, ag in TEAM_AGENTS.items():
            state = task.agent_states.get(key, STATE_PENDING)
            fields.append({"name": f"{ag['emoji']} {ag['name']}", "value": state, "inline": True})

        elapsed = int(time.time() - task.created_at)
        description = (
            f"任務 ID：`{task.task_id}`\n"
            f"耗時：{elapsed}秒\n\n"
            f"成員狀態：\n"
        )

        color = 0xFFD700  # 處理中金色
        if all(s in (STATE_DONE,) for s in task.agent_states.values()):
            color = 0x00C851
        elif any(s == STATE_ERROR for s in task.agent_states.values()):
            color = 0xFF4444

        return make_embed(
            title=f"📋 分析任務：{task.symbol}",
            description=description,
            color=color,
            footer=f"任務ID：{task.task_id}",
            fields=fields,
        )

    def parse_agent_report(content: str):
        """解析回傳格式：[Agent名稱] 任務ID 報告內容"""
        match = re.match(r"\[([^\]]+)\]\s*(\S+)\s*(.+)", content, re.DOTALL)
        if match:
            return match.group(1).strip(), match.group(2).strip(), match.group(3).strip()
        return None, None, None

    async def update_user_status(task: TaskState):
        """更新用戶側的狀態消息"""
        try:
            embed = build_status_embed(task)
            await task.status_msg.edit(embed=embed)
        except Exception as e:
            log.error(f"❌ 更新狀態失敗：{e}")

    @client.event
    async def on_ready():
        log.info(f"✅ Leader Bot 上線：{client.user}")
        await tree.sync()
        log.info("✅ 斜線命令已同步")

        team_ch = client.get_channel(team_channel_id)
        if team_ch:
            embed = make_embed(
                title="✅ Agent Leader 已上線",
                description="開始接收分析需求",
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
                # 找到對應的 agent key
                agent_key = AGENT_NAME_TO_KEY.get(agent_name)
                if agent_key and agent_key in task.agent_states:
                    # 更新為處理中
                    task.agent_states[agent_key] = STATE_PROCESSING
                    await update_user_status(task)

                    # 添加報告
                    task.reports.append((agent_name, report_text))
                    # 更新為已回傳
                    task.agent_states[agent_key] = STATE_SUMMARIZING
                    await update_user_status(task)
                    log.info(f"📥 Leader 收到 {agent_name} 報告（{task_id}）")

                    # 批次更新為完成
                    await asyncio.sleep(1)
                    task.agent_states[agent_key] = STATE_DONE
                    await update_user_status(task)
            return

        # ── 用戶頻道（DM/mention）：接收分析請求 ──
        if message.author.id == client.user.id:
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = client.user in message.mentions

        if not (is_dm or is_mentioned):
            return

        content = message.content.strip()
        if is_mentioned:
            content = re.sub(r"<@\d+>\s*", "", content)

        symbols = re.findall(r'\b([A-Z]{2,5}(?:-USD)?)\b', content.upper())
        if not symbols:
            await message.channel.send("⚠️ 請指定要分析的標的，例如：`分析 NVDA`")
            return

        symbol = symbols[0]
        task_id = f"task_{int(time.time() * 1000)}"

        # 發送初始狀態
        status_embed = make_embed(
            title=f"📋 任務已分發：分析 {symbol}",
            description="正在等待團隊回覆...\n\n成員：",
            color=0xFFD700,
            footer=f"任務ID：{task_id}",
            fields=[{"name": ag["name"], "value": STATE_SENT, "inline": True} for ag in TEAM_AGENTS.values()],
        )
        status_msg = await message.channel.send(embed=status_embed)

        team_ch = client.get_channel(team_channel_id)
        if not team_ch:
            await message.channel.send("⚠️ 無法訪問團隊頻道")
            return

        # 初始化任務
        task = TaskState(symbol, task_id, message.channel, status_msg)
        pending_tasks[task_id] = task

        # 所有 Agent 標記為已發送
        for key in task.agent_states:
            task.agent_states[key] = STATE_SENT
        await update_user_status(task)

        # 發任務到團隊頻道
        agent_list = "\n".join([f"{ag['emoji']} {ag['name']}" for ag in TEAM_AGENTS.values()])
        task_embed = make_embed(
            title=f"📋 團隊任務：分析 {symbol}",
            description=f"請各 Agent 分析並回傳報告到本頻道\n\n參與成員：\n{agent_list}",
            color=0xFFD700,
            footer=f"任務ID：{task_id}",
        )
        await team_ch.send(embed=task_embed)

        # 啟動超時監控協程
        asyncio.create_task(monitor_task(task, client))

    async def monitor_task(task, client):
        """監控任務超時，定期更新狀態"""
        start = time.time()
        receive_deadline = start + task.receive_timeout
        process_deadline = start + task.receive_timeout + task.process_timeout

        while time.time() < process_deadline:
            await asyncio.sleep(5)
            elapsed = int(time.time() - start)

            # 檢查接收超時
            if time.time() > receive_deadline:
                for key, state in task.agent_states.items():
                    if state == STATE_SENT:
                        task.agent_states[key] = STATE_TIMEOUT

            # 如果所有 Agent 都已完成或超時，結束監控
            all_done = all(
                s in (STATE_DONE, STATE_TIMEOUT, STATE_ERROR) for s in task.agent_states.values()
            )
            if all_done:
                break

        # 移除待處理並彙總回覆
        if task.task_id in pending_tasks:
            pending_tasks.pop(task.task_id)
            await summarize_and_reply(task)

    async def summarize_and_reply(task):
        """蒐集報告並回覆用戶"""
        reports = task.reports
        symbol = task.symbol

        # 最終狀態更新
        for key in task.agent_states:
            if task.agent_states[key] not in (STATE_DONE,):
                if task.agent_states[key] != STATE_DONE:
                    task.agent_states[key] = STATE_TIMEOUT
        await update_user_status(task)

        # 延遲一下確保狀態已更新
        await asyncio.sleep(2)

        # 建構最終 embed
        fields = []
        for key, ag in TEAM_AGENTS.items():
            state = task.agent_states.get(key, STATE_TIMEOUT)
            # 找到該 agent 的報告
            report_text = next((r for n, r in reports if n == ag["name"]), "（無報告）")
            fields.append({
                "name": f"{ag['emoji']} {ag['name']} {state}",
                "value": report_text[:1024] if len(report_text) > 10 else state,
                "inline": False,
            })

        embed = make_embed(
            title=f"🎯 {symbol} 綜合分析報告",
            description=f"由 **{len(reports)}** 位團隊成員分析\n耗時：{int(time.time() - task.created_at)}秒",
            color=0x00C851,
            footer="Market Monitor Agent Team",
            fields=fields,
        )

        # 簡單結論
        bullish = sum(1 for _, r in reports if any(k in r.lower() for k in ["多頭", "買入", "看多", "buy", "bull"]))
        bearish = sum(1 for _, r in reports if any(k in r.lower() for k in ["空頭", "賣出", "看空", "sell", "bear"]))

        if bullish > bearish:
            conclusion = f"🟢 **結論：偏多**（{bullish} vs {bearish}）"
        elif bearish > bullish:
            conclusion = f"🔴 **結論：偏空**（{bullish} vs {bearish}）"
        else:
            conclusion = f"⚪ **結論：中性**"

        embed.description += f"\n\n{conclusion}"

        try:
            await task.status_msg.edit(embed=embed)
        except Exception as e:
            log.error(f"❌ 發送最終報告失敗：{e}")
            await task.user_channel.send(embed=embed)

    # 斜線命令
    @tree.command(name="幫助", description="顯示使用說明")
    async def cmd_help(interaction: discord.Interaction):
        embed = make_embed(
            title="📖 Agent Team 使用說明",
            description="向對沖基金老闆一样，發送分析需求",
            fields=[
                {"name": "📋 發起分析", "value": "`@LeaderBot 分析 NVDA` 或 DM `分析 TSLA`", "inline": False},
                {"name": "👥 團隊成員", "value": "交易員、行業研究員、宏觀策略師、情報官、風控官、量化策略師", "inline": False},
                {"name": "📊 狀態說明", "value": "📤發送中 → ✅已接收 → 🔄處理中 → 📝匯總中 → ✅完成\n（超時 → ❌ 接收超時）", "inline": False},
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @tree.command(name="團隊", description="查看團隊成員")
    async def cmd_team(interaction: discord.Interaction):
        fields = [{"name": f"{ag['emoji']} {ag['name']}", "value": ag["focus"], "inline": False} for ag in TEAM_AGENTS.values()]
        embed = make_embed(title="🤖 Agent Team 成員", description="共 6 位專業分析師", color=0x00C851, fields=fields)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    log.info("🚀 啟動 Leader Bot")
    client.run(bot_token, log_handler=None)


# ══════════════════════════════════════════════════════════════
# Team Agent Bot（監聽團隊頻道，分析並回傳）
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
        """解析任務格式"""
        match = re.search(r"📋.*分析\s+([A-Z]{2,5}(?:-USD)?).*任務ID：`(\S+)`", content)
        if match:
            return match.group(1), match.group(2)
        return None, None

    @client.event
    async def on_ready():
        log.info(f"✅ {agent['name']} Bot 上線：{client.user}")
        await tree.sync()

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

        symbol, task_id = parse_task_message(message.content)
        if not symbol or not task_id:
            return

        log.info(f"📋 {agent['name']} 收到任務：{symbol}（{task_id}）")

        # 回覆收到任務（讓 Leader 知道已接收）
        await message.channel.send(
            f"[{agent['name']}] {task_id} ✅ 已接收任務，開始分析 {symbol}..."
        )

        # 獲取技術數據
        ta_data = get_ta_summary(symbol)
        if not ta_data:
            await message.channel.send(f"[{agent['name']}] {task_id} ⚠️ 無法取得 {symbol} 數據")
            log.error(f"❌ {agent['name']} 無法取得 {symbol} 數據")
            return

        # 調用 AI 分析
        user_msg = f"""請分析 {symbol} 的投資價值。

參考數據：
{ta_data}

請根據你的專業領域，給出簡潔的分析意見。"""

        async with message.channel.typing():
            report = await call_minimax(agent["system_prompt"], user_msg)

        # 回傳報告
        report_msg = f"[{agent['name']}] {task_id} {report}"
        await message.channel.send(report_msg)
        log.info(f"✅ {agent['name']} 已回傳報告（{task_id}）")
        await message.add_reaction("✅")

    # 斜線命令
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
        ta_data = get_ta_summary(symbol.upper())
        if not ta_data:
            await interaction.followup.send(f"⚠️ 無法取得 {symbol} 數據")
            return

        user_msg = f"請分析 {symbol}。\n\n參考數據：\n{ta_data}"
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
# 主程式（協調所有 Bot）
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

    # 啟動 Leader Bot
    leader_thread = threading.Thread(
        target=run_leader_bot,
        args=(leader_token, team_channel_id),
        name="LeaderBot",
        daemon=True,
    )
    leader_thread.start()
    log.info("📦 Leader Bot 已啟動")

    # 啟動各 Team Agent Bot
    agent_threads = []
    for agent_key, cfg in agents_cfg.items():
        if agent_key == "chief_strategist":
            continue  # Leader 用單獨的 token

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
