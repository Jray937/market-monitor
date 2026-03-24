#!/usr/bin/env python3
"""
Market Monitor Bot — 主入口
功能：
  1. 定時抓取 Yahoo Finance 數據
  2. 計算技術指標（RSI / MACD / MA / BB）
  3. 比對警報規則，達標即發 Discord 通知
  4. 每小時摘要報告
"""
import sys
import os
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import load_config, parse_config
from src.data_fetcher import fetch_ohlcv
from src.analyzer import compute_ta, check_alert
from src.alert_manager import AlertManager
import src.discord_bot as discord
from src.logger import setup_logger

log = setup_logger("bot")

alert_mgr = None
symbols_cfg = []
summary_interval_minutes = 60
_last_summary_time = 0


def check_and_alert(symbol_cfg, ta) -> bool:
    """檢查所有警報規則，觸發時發送 Discord"""
    global alert_mgr
    if alert_mgr is None:
        return False

    sent_any = False
    for rule in symbol_cfg.alerts:
        if alert_mgr.is_in_cooldown(symbol_cfg.symbol, rule.type):
            continue

        triggered, _ = check_alert(ta, rule.type, rule.threshold)
        if not triggered:
            continue

        price = ta.current_price
        pct = ta.pct_change

        # 根據警報類型呼叫對應函數
        ok = False
        if rule.type == "rsi_oversold":
            ok = discord.send_alert_rsi_oversold(
                ta.symbol, price, pct, ta.rsi14, rule.threshold)
        elif rule.type == "rsi_overbought":
            ok = discord.send_alert_rsi_overbought(
                ta.symbol, price, pct, ta.rsi14, rule.threshold)
        elif rule.type == "macd_cross_up":
            ok = discord.send_alert_macd_cross_up(
                ta.symbol, price, pct, ta.macd, ta.macd_signal, ta.rsi14)
        elif rule.type == "macd_cross_down":
            ok = discord.send_alert_macd_cross_down(
                ta.symbol, price, pct, ta.macd, ta.macd_signal, ta.rsi14)
        elif rule.type == "price_cross_ma200":
            direction = "up" if ta.above_ma200 else "down"
            ok = discord.send_alert_ma200_cross(
                ta.symbol, price, pct, ta.sma200, direction)
        elif rule.type == "bollinger_upper":
            ok = discord.send_alert_bollinger(
                ta.symbol, price, pct, ta.bb_upper, ta.bb_lower, "upper")
        elif rule.type == "bollinger_lower":
            ok = discord.send_alert_bollinger(
                ta.symbol, price, pct, ta.bb_upper, ta.bb_lower, "lower")

        if ok:
            alert_mgr.record_trigger(symbol_cfg.symbol, rule.type, rule.cooldown_hours)
            sent_any = True

    return sent_any


def monitor_symbol(symbol_cfg):
    sym = symbol_cfg.symbol
    df = fetch_ohlcv(sym, period="6mo", interval="1d")
    if df.empty:
        return None
    ta = compute_ta(sym, df)
    if ta is None:
        return None
    check_and_alert(symbol_cfg, ta)
    return {"symbol": sym, "market": symbol_cfg.market, "ta": ta}


def monitor_all():
    import concurrent.futures
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(monitor_symbol, cfg): cfg for cfg in symbols_cfg}
        for future in concurrent.futures.as_completed(futures, timeout=90):
            try:
                r = future.result()
                if r:
                    results.append(r)
            except Exception as e:
                log.error(f"執行失敗: {e}")
    return results


def scheduler_loop(interval_minutes: int = 15):
    global _last_summary_time
    log.info(f"🕐 排程啟動，每 {interval_minutes} 分鐘執行一次")

    # 首次執行
    results = monitor_all()
    if int(time.time()) - _last_summary_time >= summary_interval_minutes * 60:
        discord.send_summary(results)
        _last_summary_time = int(time.time())

    while True:
        time.sleep(interval_minutes * 60)
        try:
            results = monitor_all()
            if int(time.time()) - _last_summary_time >= summary_interval_minutes * 60:
                discord.send_summary(results)
                _last_summary_time = int(time.time())
            if alert_mgr:
                alert_mgr.cleanup_old(max_age_hours=48)
        except KeyboardInterrupt:
            log.info("收到中斷，正常退出")
            sys.exit(0)
        except Exception as e:
            log.error(f"輪次錯誤: {e}", exc_info=True)


def main():
    global alert_mgr, symbols_cfg, summary_interval_minutes, _last_summary_time
    log.info("=" * 50)
    log.info("Market Monitor Bot 啟動")
    log.info("=" * 50)

    # 1. 讀取設定（.yaml + 環境變數）
    raw_cfg = load_config()
    discord_cfg, monitor_cfg, symbols_cfg = parse_config(raw_cfg)
    summary_interval_minutes = monitor_cfg.summary_interval

    # 2. 初始化 Discord Webhook
    discord.init_discord(discord_cfg.webhook_url)

    # 3. 初始化警報管理器
    alert_mgr = AlertManager()

    # 4. 發送上線通知
    discord.send_online_notification(len(symbols_cfg))
    log.info(f"上線通知已發送，監控 {len(symbols_cfg)} 檔")

    # 5. 啟動排程
    try:
        scheduler_loop(monitor_cfg.interval_minutes)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
