"""
設定檔讀取
"""
import os
import yaml
from dataclasses import dataclass, field
from typing import Dict, List
from .logger import setup_logger

log = setup_logger("config")

@dataclass
class AlertRule:
    type: str          # e.g. "rsi_oversold", "macd_cross_up"
    threshold: float = 0
    cooldown_hours: float = 6

@dataclass
class SymbolConfig:
    symbol: str
    market: str        # "stock" or "crypto"
    alerts: List[AlertRule] = field(default_factory=list)

@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str

@dataclass
class MonitorConfig:
    interval_minutes: int = 15
    summary_interval: int = 60

def load_config(config_path: str = "config.yaml") -> Dict:
    """讀取並解析 config.yaml"""
    # 支援環境變數：CONFIG_PATH
    path = os.environ.get("CONFIG_PATH", config_path)

    if not os.path.exists(path):
        log.error(f"設定檔不存在：{path}，請複製 config.yaml.example 並填入你的 Telegram Bot Token")
        raise FileNotFoundError(path)

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    log.info(f"設定檔載入成功：{path}")
    return raw

def parse_config(raw: Dict) -> tuple:
    """解析設定檔回傳乾淨的結構"""
    telegram_cfg = TelegramConfig(
        bot_token=raw["telegram"]["bot_token"],
        chat_id=str(raw["telegram"]["chat_id"]),
    )

    monitor_cfg = MonitorConfig(
        interval_minutes=raw.get("monitor", {}).get("interval_minutes", 15),
        summary_interval=raw.get("monitor", {}).get("summary_interval", 60),
    )

    symbols: List[SymbolConfig] = []

    # 股票
    for item in raw.get("symbols", {}).get("stocks", []):
        symbol = item["symbol"]
        alerts = [AlertRule(**a) for a in item.get("alerts", [])]
        symbols.append(SymbolConfig(symbol=symbol, market="stock", alerts=alerts))

    # 加密貨幣
    for item in raw.get("symbols", {}).get("crypto", []):
        symbol = item["symbol"]
        alerts = [AlertRule(**a) for a in item.get("alerts", [])]
        symbols.append(SymbolConfig(symbol=symbol, market="crypto", alerts=alerts))

    return telegram_cfg, monitor_cfg, symbols
