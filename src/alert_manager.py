"""
警報管理器：冷卻邏輯 + 狀態追蹤
"""
import json
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Optional
from .logger import setup_logger

log = setup_logger("alert_manager")

STATE_FILE = os.environ.get("STATE_FILE", "alert_state.json")
COOLDOWN_LOCK = threading.Lock()


@dataclass
class AlertEntry:
    symbol: str
    alert_type: str
    triggered_at: float  # unix timestamp
    cooldown_hours: float
    message: str = ""


class AlertManager:
    """記憶體 + 磁碟持久化的警報冷卻管理器"""

    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self._alerts: dict[str, AlertEntry] = {}  # key: "symbol|type"
        self._load()

    def _make_key(self, symbol: str, alert_type: str) -> str:
        return f"{symbol.upper()}|{alert_type}"

    def _load(self):
        """從磁碟恢復狀態"""
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            for key, entry in data.items():
                self._alerts[key] = AlertEntry(**entry)
            log.info(f"已載入 {len(self._alerts)} 筆記錄")
        except Exception as e:
            log.warning(f"無法載入狀態檔：{e}")

    def _save(self):
        """寫入磁碟"""
        try:
            data = {k: vars(v) for k, v in self._alerts.items()}
            with open(self.state_file, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"寫入狀態檔失敗：{e}")

    def is_in_cooldown(self, symbol: str, alert_type: str) -> bool:
        """檢查是否在冷卻期內"""
        key = self._make_key(symbol, alert_type)
        with COOLDOWN_LOCK:
            if key not in self._alerts:
                return False
            entry = self._alerts[key]
            elapsed = (time.time() - entry.triggered_at) / 3600  # 小時
            if elapsed < entry.cooldown_hours:
                remaining = entry.cooldown_hours - elapsed
                log.debug(f"{key} 冷卻中，剩餘 {remaining:.1f}h")
                return True
            else:
                # 冷卻期已過，移除
                del self._alerts[key]
                return False

    def record_trigger(self, symbol: str, alert_type: str, cooldown_hours: float, message: str = ""):
        """記錄觸發事件"""
        key = self._make_key(symbol, alert_type)
        with COOLDOWN_LOCK:
            self._alerts[key] = AlertEntry(
                symbol=symbol.upper(),
                alert_type=alert_type,
                triggered_at=time.time(),
                cooldown_hours=cooldown_hours,
                message=message,
            )
        self._save()
        log.info(f"📝 記錄警報：{key}，冷卻 {cooldown_hours}h")

    def cleanup_old(self, max_age_hours: float = 48):
        """清理過期記錄"""
        now = time.time()
        removed = 0
        with COOLDOWN_LOCK:
            for key in list(self._alerts.keys()):
                entry = self._alerts[key]
                age = (now - entry.triggered_at) / 3600
                if age > max_age_hours:
                    del self._alerts[key]
                    removed += 1
        if removed:
            log.info(f"清理了 {removed} 筆過期警報")
            self._save()
        return removed
