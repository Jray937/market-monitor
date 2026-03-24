"""
Yahoo Finance 數據獲取
使用 yfinance，完全免費
"""
import yfinance as yf
import pandas as pd
import time as _time

from .logger import setup_logger

log = setup_logger("data_fetcher")

_cache: dict = {}
_CACHE_TTL_SEC = 60


def _cache_key(symbol: str, period: str, interval: str) -> str:
    return f"{symbol}_{period}_{interval}"


def fetch_ohlcv(symbol: str, period: str = "3mo", interval: str = "1d") -> pd.DataFrame:
    key = _cache_key(symbol, period, interval)
    now = _time.time()
    if key in _cache and (now - _cache[key]["ts"]) < _CACHE_TTL_SEC:
        log.debug(f"快取命中：{symbol}")
        return _cache[key]["data"].copy()

    log.info(f"抓取數據：{symbol} | period={period} interval={interval}")
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval, auto_adjust=True)
        if df.empty:
            log.warning(f"{symbol} 無數據返回")
            return pd.DataFrame()
        required = ["Open", "High", "Low", "Close", "Volume"]
        for col in required:
            if col not in df.columns:
                log.warning(f"{symbol} 缺少欄位：{col}")
                return pd.DataFrame()
        df = df.dropna()
        df = df[df["Volume"] > 0]
        _cache[key] = {"data": df, "ts": now}
        log.info(f"✅ {symbol} 取得 {len(df)} 根 K 線，最新：{df.index[-1]}")
        return df.copy()
    except Exception as e:
        log.error(f"❌ {symbol} 抓取失敗：{e}")
        return pd.DataFrame()


def fetch_current_price(symbol: str) -> float | None:
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = info.last_price or info.regular_price
        if price:
            return float(price)
    except Exception:
        pass
    df = fetch_ohlcv(symbol, period="5d", interval="1d")
    if not df.empty:
        return float(df["Close"].iloc[-1])
    return None


def is_market_open() -> bool:
    import datetime as dt
    now_utc = dt.datetime.utcnow()
    if now_utc.weekday() >= 5:
        return False
    mins = now_utc.hour * 60 + now_utc.minute
    return (14 * 60 + 30) <= mins < (21 * 60)
