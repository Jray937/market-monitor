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

AGENT_NAME_TO_KEY = {ag["name"]: key for key, ag in TEAM_AGENTS.items()}

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


# ══════════════════════════════════════════════════════════════
# Leader Bot — LLM 驅動的智能調度員
# ══════════════════════════════════════════════════════════════

# Leader 的系統提示：用於理解用戶需求 + 決定調度策略
LEADER_SYSTEM_PROMPT = """你是一個智能投資研究團隊的領導者（Agent Leader）。
團隊成員：
- 📊 交易員(trader)：技術分析、进出场点位
- 📈 行業研究員(sector_analyst)：基本面、行业、估值
- 🌍 宏觀策略師(macro_strategist)：宏觀經濟、政策
- 📰 情報官(intelligence_officer)：新聞、市場情緒
- ⚠️ 風控官(risk_officer)：風險評估、倉位
- 🔢 量化策略師(quant_strategist)：量化信號、統計

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

    def build_task_message(task_id: str, dispatch_task: str, agents_to_dispatch: list):
        """構建團隊任務的 content 文字與 embed，供 on_message 和 /ask 共用"""
        agent_list = "\n".join([
            f"{TEAM_AGENTS[k]['emoji']} {TEAM_AGENTS[k]['name']}"
            for k in agents_to_dispatch
        ])
        task_content = (
            f"📋 團隊任務：{dispatch_task}\n\n"
            f"參與成員：{', '.join([TEAM_AGENTS[k]['name'] for k in agents_to_dispatch])}\n\n"
            f"任務ID：{task_id}"
        )
        task_embed = make_embed(
            title="📋 團隊任務",
            description=(
                f"**任務描述：**\n{dispatch_task}\n\n"
                f"**參與成員：**\n{agent_list}\n\n"
                f"請各 Agent 根據自身專業領域提供分析，並回傳報告到本頻道。"
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
                    # 標記為處理中 → 完成
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

        # 初始化所有相關 Agent 狀態
        for key in task.agent_states:
            task.agent_states[key] = STATE_PENDING
        for key in agents_to_dispatch:
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

        # 發任務到團隊頻道（同時附帶 content 文字，讓 Agent 能解析）
        task_content, task_embed = build_task_message(task_id, dispatch_task, agents_to_dispatch)
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

        # 啟動超時監控
        asyncio.create_task(monitor_task(task, client))

    async def monitor_task(task, client):
        start = time.time()
        receive_deadline = start + task.receive_timeout
        process_deadline = start + task.receive_timeout + task.process_timeout

        while time.time() < process_deadline:
            await asyncio.sleep(5)

            if time.time() > receive_deadline:
                for key in task.dispatch_agents:
                    if task.agent_states[key] == STATE_SENT:
                        task.agent_states[key] = STATE_TIMEOUT

            all_done = all(
                task.agent_states[key] in (STATE_DONE, STATE_TIMEOUT, STATE_ERROR)
                for key in task.dispatch_agents
            )
            if all_done:
                break

        if task.task_id in pending_tasks:
            pending_tasks.pop(task.task_id)
            await summarize_and_reply(task)

    async def summarize_and_reply(task):
        reports = task.reports

        # 最終狀態
        for key in task.dispatch_agents:
            if task.agent_states[key] not in (STATE_DONE,):
                if task.agent_states[key] == STATE_SENT:
                    task.agent_states[key] = STATE_TIMEOUT
        await update_user_status(task)
        await asyncio.sleep(1)

        # 建構最終報告
        fields = []
        for key in task.dispatch_agents:
            ag = TEAM_AGENTS[key]
            state = task.agent_states.get(key, STATE_TIMEOUT)
            report_text = next((r for n, r in reports if n == ag["name"]), "（無報告）")
            fields.append({
                "name": f"{ag['emoji']} {ag['name']} {state}",
                "value": report_text[:1024] if len(report_text) > 10 else state,
                "inline": False,
            })

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

        embed = make_embed(
            title=f"🎯 分析報告（{len(reports)}/{len(task.dispatch_agents)} 位成員回覆）",
            description=(
                f"📌 任務：{task.dispatch_task}\n\n"
                f"⏱ 耗時：{int(time.time() - task.created_at)}秒\n\n"
                + (f"📝 結論：{conclusion}\n\n" if conclusion else "")
            ),
            color=0x00C851,
            footer="Market Monitor Agent Team",
            fields=fields,
        )

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

        # 建立狀態訊息
        fields = []
        for key in agents_to_dispatch:
            ag = TEAM_AGENTS[key]
            fields.append({"name": f"{ag['emoji']} {ag['name']}", "value": STATE_SENT, "inline": True})

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
        for key in task.agent_states:
            task.agent_states[key] = STATE_PENDING
        for key in agents_to_dispatch:
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

        task_content, task_embed = build_task_message(task_id, dispatch_task, agents_to_dispatch)
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

        log.info(f"📋 {agent['name']} 收到任務：{task_id} — {task_desc[:50] if task_desc else ''}")

        # 回傳已接收確認
        await message.channel.send(
            f"[{agent['name']}] {task_id} ✅ 已接收任務，開始分析..."
        )

        # 嘗試從任務描述中提取標的
        symbol_match = re.search(r'\b([A-Z]{2,5}(?:-USD)?)\b', task_desc or "")
        symbol = symbol_match.group(1) if symbol_match else None

        ta_data = None
        if symbol:
            ta_data = get_ta_summary(symbol)

        # 構造分析消息
        if ta_data:
            user_msg = f"""{task_desc}

參考數據：
{ta_data}

請根據你的專業領域，提供針對性的分析。保持簡潔、專業、有數據支撐。"""
        else:
            user_msg = f"""{task_desc}

請根據你的專業領域，提供深入分析。如果需要特定市場數據，請說明。
保持簡潔、專業、有數據支撐。"""

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
