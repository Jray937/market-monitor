#!/usr/bin/env python3
"""
Market Monitor Bot — 主入口
"""
import sys, os, time, datetime
from typing import List
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import load_config, parse_config
from src.data_fetcher import fetch_ohlcv
from src.analyzer import compute_ta, check_alert
from src.alert_manager import AlertManager
from src.telegram_bot import init_bot, send_alert, build_alert_message, build_summary_message
from src.logger import setup_logger

log = setup_logger("bot")
alert_mgr = None
symbols_cfg = []
summary_interval_minutes = 60
_last_summary_time = 0

def check_and_alert(symbol_cfg, ta):
    global alert_mgr
    if alert_mgr is None: return False
    sent_any = False
    for rule in symbol_cfg.alerts:
        if alert_mgr.is_in_cooldown(symbol_cfg.symbol, rule.type):
            continue
        triggered, msg = check_alert(ta, rule.type, rule.threshold)
        if not triggered: continue
        alert_text = build_alert_message(
            ta.symbol, ta.current_price, ta.pct_change,
            {"rsi14": ta.rsi14, "macd": ta.macd, "macd_signal": ta.macd_signal,
             "bb_upper": ta.bb_upper, "bb_lower": ta.bb_lower, "sma200": ta.sma200},
            msg, rule.type
        )
        ok = send_alert(alert_text)
        if ok:
            alert_mgr.record_trigger(symbol_cfg.symbol, rule.type, rule.cooldown_hours, msg)
            sent_any = True
    return sent_any

def monitor_symbol(symbol_cfg):
    sym = symbol_cfg.symbol
    df = fetch_ohlcv(sym, period="6mo", interval="1d")
    if df.empty: return None
    ta = compute_ta(sym, df)
    if ta is None: return None
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
                if r: results.append(r)
            except Exception as e:
                log.error(f"執行失敗: {e}")
    return results

def run_summary(results):
    if not results: return
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    send_alert(build_summary_message(results, now_utc))

def main():
    global alert_mgr, symbols_cfg, summary_interval_minutes, _last_summary_time
    log.info("Market Monitor Bot 啟動")
    raw_cfg = load_config()
    telegram_cfg, monitor_cfg, symbols_cfg = parse_config(raw_cfg)
    summary_interval_minutes = monitor_cfg.summary_interval
    init_bot(telegram_cfg.bot_token, telegram_cfg.chat_id)
    alert_mgr = AlertManager()
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    send_alert(f"✅ <b>Market Monitor 上線</b>\n\n🕐 {now_utc}\n\n監控中：{len(symbols_cfg)} 檔資産")
    log.info(f"上線通知已發送，監控 {len(symbols_cfg)} 檔")

    interval = monitor_cfg.interval_minutes
    log.info(f"🕐 排程启动，每 {interval} 分鐘執行")
    # 首次執行
    results = monitor_all()
    if int(time.time()) - _last_summary_time >= summary_interval_minutes * 60:
        run_summary(results)
        _last_summary_time = int(time.time())

    while True:
        time.sleep(interval * 60)
        try:
            results = monitor_all()
            if int(time.time()) - _last_summary_time >= summary_interval_minutes * 60:
                run_summary(results)
                _last_summary_time = int(time.time())
            if alert_mgr:
                alert_mgr.cleanup_old(max_age_hours=48)
        except KeyboardInterrupt:
            log.info("收到中斷，正常退出")
            sys.exit(0)
        except Exception as e:
            log.error(f"輪次錯誤: {e}", exc_info=True)

if __name__ == "__main__":
    main()
