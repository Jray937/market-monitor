"""
Polygon.io 期權數據獲取

支援版本：
  - 免費版（默認）：`POLYGON_API_TIER=free` 或未設定
  - Pro 版：`POLYGON_API_TIER=pro`

版本區分邏輯：
  - Pro：`list_options_contracts` 返回 ListResponse 對象，屬性 `.results` 是 list
  - Free：返回普通 list，直接迭代

環境變數：
  POLYGON_API_KEY   — API Key（必要）
  POLYGON_API_TIER  — `free`（默認）或 `pro`
"""
import os
import sys
from typing import Optional
from datetime import datetime, timedelta

from polygon import RESTClient
from .logger import setup_logger

log = setup_logger("options_fetcher")

_client: Optional[RESTClient] = None
_API_TIER: str = "free"


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


def _to_contract_list(resp) -> list:
    """
    統一響應轉換為 list（永遠返回 list，可安全重複迭代）

    支持類型：
      - list：直接返回
      - generator / iterator：完全迭代後返回 list（避免一次性問題）
      - Pro 回應對象（有 .results）：迭代 .results 返回 list
    """
    # Pro 版：回應對象有 .results
    if hasattr(resp, "results") and not isinstance(resp, (list, tuple, set, frozenset)):
        try:
            it = iter(resp.results)
        except (TypeError, AttributeError):
            it = iter([])
        return list(it)

    # 已是 list / tuple / set
    if isinstance(resp, (list, tuple, set, frozensist)):
        return list(resp)

    # generator / iterator：完全迭代後轉 list（關鍵修復！）
    try:
        it = iter(resp)
        return list(it)
    except TypeError:
        pass

    raise TypeError(
        f"無法解析 Polygon 回應類型，期望 list/generator，實際：{type(resp).__name__}。"
        f"請確認 POLYGON_API_TIER 是否正確（當前：{_API_TIER}）"
    )


def _stock_to_underlying(ticker: str) -> str:
    """股票代碼標準化"""
    t = ticker.upper().replace("-", "")
    if "-USD" in ticker.upper():
        return t
    return t


def get_option_expirations(ticker: str) -> list[dict]:
    """
    取得股票的所有期權到期日

    輸入：ticker: str  — 股票代碼，例如 "AAPL"、"TSLA"
    輸出：list[dict]  — 每項含 {"date": "YYYY-MM-DD", "days_to_expiry": int}
    """
    client = _init_client()
    sym = _stock_to_underlying(ticker)
    try:
        resp = client.list_options_contracts(
            underlying_ticker=sym,
            expiration_date_gte=datetime.utcnow().strftime("%Y-%m-%d"),
            expiration_date_lte=(datetime.utcnow() + timedelta(days=365)).strftime("%Y-%m-%d"),
            limit=500,
        )
    except Exception as e:
        log.error(f"[get_option_expirations] API 調用失敗：{e}")
        return []

    contracts = _to_contract_list(resp)
    expirations: dict = {}
    for c in contracts:
        if not c or not isinstance(c, dict):
            continue
        exp = c.get("expiration_date")
        if not exp:
            continue
        if exp not in expirations:
            expirations[exp] = {
                "date": exp,
                "days_to_expiry": max(0, (datetime.strptime(exp, "%Y-%m-%d") - datetime.utcnow()).days),
            }

    result = sorted(expirations.values(), key=lambda x: x["date"])
    log.info(f"[get_option_expirations] {sym} → {len(result)} 個到期日")
    return result


def get_option_chain(ticker: str, expiration_date: str) -> dict:
    """
    取得指定到期日的完整期權鏈

    輸入：
      ticker: str          — 股票代碼，例如 "AAPL"
      expiration_date: str — 到期日，格式 "YYYY-MM-DD"
    輸出：dict — {
        "calls": list[dict],
        "puts":  list[dict],
        "underlying_price": float,
        "expiry": str,
      }
      每個合約項含：{strike, last, iv, oi, delta, gamma, theta, vega, itm, intrinsic, premium}
    """
    client = _init_client()
    sym = _stock_to_underlying(ticker)

    underlying_price: float = 0.0
    try:
        ticker_resp = client.get_ticker(sym)
        underlying_price = float(ticker_resp.last.price)
    except Exception as e:
        log.warning(f"[get_option_chain] 取得標的現價失敗：{e}")

    try:
        resp = client.list_options_contracts(
            underlying_ticker=sym,
            expiration_date=expiration_date,
            limit=1000,
        )
    except Exception as e:
        log.error(f"[get_option_chain] 取得期權合約列表失敗：{e}")
        return {"calls": [], "puts": [], "underlying_price": underlying_price, "expiry": expiration_date}

    contracts = _to_contract_list(resp)
    calls, puts = [], []

    for c in contracts:
        if not c or not isinstance(c, dict):
            continue
        strike: float = float(c.get("strike_price") or 0)
        ct: str = c.get("contract_type", "")
        if ct == "call":
            calls.append(_build_entry(c, underlying_price, strike))
        elif ct == "put":
            puts.append(_build_entry(c, underlying_price, strike))

    calls.sort(key=lambda x: x["strike"])
    puts.sort(key=lambda x: x["strike"])
    log.info(f"[get_option_chain] {sym} {expiration_date} → {len(calls)} calls, {len(puts)} puts")
    return {
        "calls": calls,
        "puts": puts,
        "underlying_price": underlying_price,
        "expiry": expiration_date,
    }


def _build_entry(c: dict, underlying_price: float, strike: float) -> dict:
    """
    將 Polygon 合約 dict 轉換為統一格式
    輸入：c: dict, underlying_price: float, strike: float
    輸出：dict — 含所有 Greeks + 報價欄位
    """
    last_price: float = float(c.get("last_trade_price") or c.get("last") or 0)
    iv: float = float(c.get("implied_volatility") or 0)
    oi: int = int(c.get("open_interest") or 0)
    delta_val: float = float(c.get("delta") or 0)
    gamma_val: float = float(c.get("gamma") or 0)
    theta_val: float = float(c.get("theta") or 0)
    vega_val: float = float(c.get("vega") or 0)
    ct: str = c.get("contract_type", "")
    itm = "ITM" if underlying_price > strike else ("OTM" if underlying_price < strike else "ATM")
    intrinsic = max(0, underlying_price - strike) if ct == "call" else max(0, strike - underlying_price)
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
    輸入：ticker: str, expiration_date: str
    輸出：dict — {
        "ticker": str,
        "expiry": str,
        "underlying_price": float,
        "atm_strike": float,
        "oi_wall": dict | None,
        "total_calls_oi": int,
        "total_puts_oi": int,
        "calls": list,
        "puts": list,
      }
    """
    chain = get_option_chain(ticker, expiration_date)
    underlying_price = chain.get("underlying_price", 0)
    calls = chain.get("calls", [])
    puts = chain.get("puts", [])

    all_oi = []
    for c in calls:
        if c["oi"] > 0:
            all_oi.append({"strike": c["strike"], "oi": c["oi"], "type": "call"})
    for p in puts:
        if p["oi"] > 0:
            all_oi.append({"strike": p["strike"], "oi": p["oi"], "type": "put"})

    oi_wall = max(all_oi, key=lambda x: x["oi"]) if all_oi else None
    all_strikes = sorted({c["strike"] for c in calls} | {p["strike"] for p in puts})
    atm_strike = float(min(all_strikes, key=lambda s: abs(s - underlying_price))) if underlying_price and all_strikes else 0.0

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
