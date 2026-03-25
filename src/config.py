"""
設定讀取：環境變數優先（Railway 安全），其次 config.yaml
"""
import os
import yaml
from dataclasses import dataclass, field
from typing import List, Optional
from .logger import setup_logger

log = setup_logger("config")


@dataclass
class AlertRule:
    type: str
    threshold: float = 0
    cooldown_hours: float = 6


@dataclass
class SymbolConfig:
    symbol: str
    market: str        # "stock" or "crypto"
    alerts: List[AlertRule] = field(default_factory=list)


@dataclass
class MonitorConfig:
    interval_minutes: int = 15
    summary_interval: int = 60


@dataclass
class AgentSettings:
    """單一 Agent 配置"""
    enabled: bool = True
    token_env: str = ""          # 環境變數名（用於讀取 Discord Token）
    watch_symbols: list = field(default_factory=list)  # 該 Agent 關注的標的


def load_config(config_path: str = "config.yaml") -> dict:
    path = os.environ.get("CONFIG_PATH", config_path)
    if not os.path.exists(path):
        log.warning(f"設定檔不存在：{path}，使用環境變數")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    log.info(f"設定檔載入：{path}")
    return raw


def parse_config(raw: dict) -> tuple:
    """解析設定檔，返回 (monitor_cfg, symbols)"""
    bot_token = os.environ.get("DISCORD_BOT_TOKEN")
    if not bot_token:
        raise ValueError(
            "❌ 缺少 DISCORD_BOT_TOKEN\n"
            "請在 Railway 環境變數設定 DISCORD_BOT_TOKEN"
        )

    monitor_cfg = MonitorConfig(
        interval_minutes=raw.get("monitor", {}).get("interval_minutes", 15),
        summary_interval=raw.get("monitor", {}).get("summary_interval", 60),
    )

    symbols: List[SymbolConfig] = []
    for item in raw.get("symbols", {}).get("stocks", []):
        alerts = [AlertRule(**a) for a in item.get("alerts", [])]
        symbols.append(SymbolConfig(symbol=item["symbol"], market="stock", alerts=alerts))
    for item in raw.get("symbols", {}).get("crypto", []):
        alerts = [AlertRule(**a) for a in item.get("alerts", [])]
        symbols.append(SymbolConfig(symbol=item["symbol"], market="crypto", alerts=alerts))

    return monitor_cfg, symbols


def load_agents_config(raw: dict) -> dict[str, AgentSettings]:
    """從設定檔解析多 Agent 配置（向後相容）"""
    agents_raw = raw.get("agents", {})
    agents = {}

    for name, cfg in agents_raw.items():
        if isinstance(cfg, dict):
            agents[name] = AgentSettings(
                enabled=cfg.get("enabled", True),
                token_env=cfg.get("token_env", f"{name.upper()}_TOKEN"),
                watch_symbols=cfg.get("watch_symbols", []),
            )
        else:
            agents[name] = AgentSettings(enabled=bool(cfg))

    return agents
