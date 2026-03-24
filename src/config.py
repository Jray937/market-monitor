"""
設定讀取：環境變數優先（Railway 安全），其次 config.yaml
"""
import os
import yaml
from dataclasses import dataclass, field
from typing import Dict, List
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
class DiscordConfig:
    webhook_url: str

@dataclass
class MonitorConfig:
    interval_minutes: int = 15
    summary_interval: int = 60

def load_config(config_path: str = "config.yaml") -> Dict:
    path = os.environ.get("CONFIG_PATH", config_path)
    if not os.path.exists(path):
        log.warning(f"設定檔不存在：{path}，使用環境變數")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    log.info(f"設定檔載入：{path}")
    return raw

def parse_config(raw: Dict) -> tuple:
    # Discord：環境變數優先，config.yaml 其次
    discord_cfg = DiscordConfig(
        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL") or raw.get("discord", {}).get("webhook_url", ""),
    )
    if not discord_cfg.webhook_url:
        raise ValueError(
            "❌ 缺少 Discord Webhook URL！\n"
            "請在 Railway 環境變數設定 DISCORD_WEBHOOK_URL\n"
            "或填入 config.yaml 的 discord.webhook_url"
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

    return discord_cfg, monitor_cfg, symbols
