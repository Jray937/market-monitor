"""
Polygon.io 期權數據獲取

環境變數：
  POLYGON_API_KEY  — API Key
  POLYGON_API_TIER — free（默認）或 pro

功能：
  - 60s 記憶體快取（相同標的+到期日不重複請求）
  - 指數退讓 + 429 rate limit 處理
  - ITM/OTM/ATM 自動判定
  - generator 永遠轉 list（避免一次性問題）

版本區分邏輯：
  - Pro 版：返回 ListResponse 對象，屬性 `.results` 是 list
  - Free 版：返回普通 list，直接迭代
"""
import os
import time as _time_module
import hashlib
from typing import Optional
from datetime import datetime, timedelta
from polygon import RESTClient
from .logger import setup_logger

log = setup_logger("options_fetcher")

_client: Optional[RESTClient] = None
_API_TIER: str = "free"

# ── 快取（60s TTL）────────────────────────────
_CACHE: dict = {}
_CACHE_TTL: float = 60.0


def _ts() -> float:
    """當前時間浮點數（避免 time 參數 shadow）"""
    return _time_module.time()


def _sleep(seconds: float):
    """sleep（避免 time 參數 shadow）"""
    _time_module.sleep(seconds)


def _cache_key(prefix: str, **kwargs) -> str:
    parts = [prefix] + [
        f"{k}={sorted(v) if isinstance(v, list) else v}"
        for k, v in sorted(kwargs.items())
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _get_cached(key: str):
    if key not in _CACHE:
        return None
    entry = _CACHE[key]
    if _ts() - entry["ts"] > _CACHE_TTL:
        del _CACHE[key]
        return None
    return entry["data"]


def _set_cached(key: str, data):
    _CACHE[key] = {"data": data, "ts": _ts()}


# ── 客戶端初始化 ────────────────────────────────

def _init_client() -> RESTClient:
    """初始化並快取 Polygon 客戶端"""
    global _client, _API_TIER
    if _client is not None:
        return _client
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        raise ValueError("❌ 缺少 POLYGON_API_KEY，請在 Railway 環境變數設定")
    _API_TIER = os.environ.get("POLYGON_API_TIER", "free").lower()
    _client = RESTClient(api_key)
    log.info(f"Polygon client 初始化 | Tier: {_API_TIER} | Key: ***{api_key[-4:]}")
    return _client


def _to_list(resp) -> list:
    """永遠返回 list，避免 generator 一次性問題"""
    if hasattr(resp, "results") and not isinstance(resp, (list, tuple, set, frozenset)):
        try:
            return list(resp.results)
        except (TypeError, AttributeError):
            return []
    if isinstance(resp, (list, tuple, set, frozenset)):
        return list(resp)
    try:
        return list(resp)
    except TypeError:
        raise TypeError(f"無法解析 Polygon 回應：{type(resp).__name__}")


def _stock_to_underlying(ticker: str) -> str:
    t = ticker.upper().replace("-", "")
    return t if "-USD" in ticker.upper() else t


# ── 通用：帶指數退讓的 API 請求 ───────────────

def _get_with_retry(method: str, **kwargs) -> Optional[list]:
    """
    請求封裝：附帶指數退讓 + 429 處理
    最多 4 次：1s → 2s → 4s → 8s
    """
    attempt = 0
    max_attempts = 4
    while attempt < max_attempts:
        try:
            client = _init_client()
            resp = getattr(client, method)(**kwargs)
            return _to_list(resp)
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "rate" in err or "too many" in err:
                wait = 2 ** attempt
                log.warning(f"Rate limit（{attempt+1}/{max_attempts}），等 {wait}s...")
                _sleep(wait)
                attempt += 1
                continue
            log.error(f"API 錯誤 [{method}]：{e}")
            return None
    log.error(f"API 請求全部失敗（{max_attempts} 次）")
    return None


# ── 取得標的現價 ─────────────────────────────

def _get_underlying_price(ticker: str) -> float:
    """用 get_previous_close 取得前一交易日收盤價（快取 60s）"""
    key = _cache_key("price", ticker=ticker)
    cached = _get_cached(key)
    if cached is not None:
        return cached
    sym = _stock_to_underlying(ticker)
    resp = _get_with_retry("get_previous_close_agg", ticker=sym)
    price = 0.0
    if resp:
        try:
            if hasattr(resp[0], 'close'):
                price = float(resp[0].close)
            else:
                price = float(resp[0].get("c") or resp[0].get("close") or 0)
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
    _set_cached(key, price)
    if price:
        log.info(f"現價 {sym} = ${price}")
    return price


# ── 公開 API ───────────────────────────────────

def get_option_expirations(ticker: str) -> list[dict]:
    """
    取得股票所有期權到期日（快取 60s）
    返回：[{date: str, days_to_expiry: int}, ...]
    """
    key = _cache_key("expirations", ticker=ticker)
    cached = _get_cached(key)
    if cached is not None:
        return cached
    sym = _stock_to_underlying(ticker)
    resp = _get_with_retry(
        "list_options_contracts",
        underlying_ticker=sym,
        expiration_date_gte=datetime.utcnow().strftime("%Y-%m-%d"),
        expiration_date_lte=(datetime.utcnow() + timedelta(days=180)).strftime("%Y-%m-%d"),
        limit=500,
    )
    if resp is None:
        return []
    expirations: dict = {}
    for c in resp:
        if not c or not isinstance(c, dict):
            continue
        exp = c.get("expiration_date")
        if not exp or exp in expirations:
            continue
        try:
            days = max(0, (datetime.strptime(exp, "%Y-%m-%d") - datetime.utcnow()).days)
        except Exception:
            days = 0
        expirations[exp] = {"date": exp, "days_to_expiry": days}
    result = sorted(expirations.values(), key=lambda x: x["date"])
    _set_cached(key, result)
    log.info(f"[get_option_expirations] {sym} → {len(result)} 個到期日")
    return result


def get_option_chain(ticker: str, expiration_date: str) -> dict:
    """
    取得指定到期日的完整期權鏈（快取 60s）
    返回：{calls: [], puts: [], underlying_price: float, expiry: str}
    """
    key = _cache_key("chain", ticker=ticker, expiry=expiration_date)
    cached = _get_cached(key)
    if cached is not None:
        return cached
    sym = _stock_to_underlying(ticker)
    underlying_price = _get_underlying_price(sym)
    resp = _get_with_retry(
        "list_options_contracts",
        underlying_ticker=sym,
        expiration_date=expiration_date,
        limit=500,
    )
    calls, puts = [], []
    if resp:
        for c in resp:
            if not c or not isinstance(c, dict):
                continue
            strike = float(c.get("strike_price") or 0)
            ct = c.get("contract_type", "")
            entry = _build_entry(c, underlying_price, strike)
            if ct == "call":
                calls.append(entry)
            elif ct == "put":
                puts.append(entry)
    calls.sort(key=lambda x: x["strike"])
    puts.sort(key=lambda x: x["strike"])
    result = {
        "calls": calls,
        "puts": puts,
        "underlying_price": underlying_price,
        "expiry": expiration_date,
    }
    _set_cached(key, result)
    log.info(f"[get_option_chain] {sym} {expiration_date} → {len(calls)} calls, {len(puts)} puts")
    return result


def _build_entry(c: dict, underlying_price: float, strike: float) -> dict:
    last_price = float(c.get("last_trade_price") or c.get("last") or 0)
    iv = float(c.get("implied_volatility") or 0)
    oi = int(c.get("open_interest") or 0)
    delta_val = float(c.get("delta") or 0)
    gamma_val = float(c.get("gamma") or 0)
    theta_val = float(c.get("theta") or 0)
    vega_val = float(c.get("vega") or 0)
    ct = c.get("contract_type", "")
    if ct == "call":
        itm = "ITM" if underlying_price > strike else ("OTM" if underlying_price < strike else "ATM")
        intrinsic = max(0.0, underlying_price - strike)
    elif ct == "put":
        itm = "ITM" if underlying_price < strike else ("OTM" if underlying_price > strike else "ATM")
        intrinsic = max(0.0, strike - underlying_price)
    else:
        itm = "?"
        intrinsic = 0.0
    return {
        "strike": strike,
        "last": last_price,
        "iv": round(iv * 100, 2),
        "oi": oi,
        "delta": round(delta_val, 4),
        "gamma": round(gamma_val, 6),
        "theta": round(theta_val, 4),
        "vega": round(vega_val, 4),
        "itm": itm,
        "intrinsic": round(intrinsic, 2),
        "premium": round(last_price * 100, 2),
    }


def build_options_wall(ticker: str, expiration_date: str) -> dict:
    """
    構建期權牆（OI Wall）
    返回：{ticker, expiry, underlying_price, atm_strike, oi_wall,
           total_calls_oi, total_puts_oi, calls, puts}
    """
    chain = get_option_chain(ticker, expiration_date)
    underlying_price = chain.get("underlying_price", 0)
    calls, puts = chain.get("calls", []), chain.get("puts", [])
    all_oi = (
        [{"strike": c["strike"], "oi": c["oi"], "type": "call"} for c in calls if c["oi"] > 0]
        + [{"strike": p["strike"], "oi": p["oi"], "type": "put"}  for p in puts if p["oi"] > 0]
    )
    oi_wall = max(all_oi, key=lambda x: x["oi"]) if all_oi else None
    all_strikes = sorted({c["strike"] for c in calls} | {p["strike"] for p in puts})
    atm_strike = 0.0
    if underlying_price and all_strikes:
        atm_strike = float(min(all_strikes, key=lambda s: abs(s - underlying_price)))
    return {
        "ticker": ticker.upper(),
        "expiry": expiration_date,
        "underlying_price": underlying_price,
        "atm_strike": atm_strike,
        "oi_wall": oi_wall,
        "total_calls_oi": sum(c["oi"] for c in calls),
        "total_puts_oi": sum(p["oi"] for p in puts),
        "calls": calls,
        "puts": puts,
    }
