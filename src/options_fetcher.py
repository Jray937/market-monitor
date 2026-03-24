"""
Polygon.io 期權數據獲取
免費版支援：歷史 K 線（5 年）、基本期權到期日、期權報價（延遲 15 分鐘）
"""
import os
import sys
from typing import Optional
from datetime import datetime, timedelta

from polygon import RESTClient
from .logger import setup_logger

log = setup_logger("options_fetcher")

# ── Client 單例 ──
_client: Optional[RESTClient] = None


def get_client() -> RESTClient:
    global _client
    if _client is None:
        api_key = os.environ.get("POLYGON_API_KEY")
        if not api_key:
            raise ValueError("❌ 缺少 POLYGON_API_KEY，請在 Railway 環境變數設定")
        _client = RESTClient(api_key)
        log.info("Polygon client 初始化完成")
    return _client


# ── 期權到期日 ──

def get_option_expirations(ticker: str) -> list[dict]:
    """
    取得股票的所有期權到期日
    免費版：返回未來 12 個月內的每月到期日
    """
    client = get_client()
    sym = _stock_to_underlying(ticker)

    try:
        # 拿期權鏈（當月 + 下月）
        resp = client.list_options_contracts(
            underlying_ticker=sym,
            expiration_date_gte=datetime.utcnow().strftime("%Y-%m-%d"),
            expiration_date_lte=(datetime.utcnow() + timedelta(days=365)).strftime("%Y-%m-%d"),
            limit=100,
        )
        expirations = {}
        for c in resp:
            if c and c.get("expiration_date"):
                exp = c["expiration_date"]
                if exp not in expirations:
                    expirations[exp] = {
                        "date": exp,
                        "days_to_expiry": max(0, (datetime.strptime(exp, "%Y-%m-%d") - datetime.utcnow()).days),
                    }
        result = sorted(expirations.values(), key=lambda x: x["date"])
        log.info(f"{sym} 找到 {len(result)} 個到期日")
        return result
    except Exception as e:
        log.error(f"取得期權到期日失敗：{e}")
        return []


def _stock_to_underlying(ticker: str) -> str:
    """轉換股票代碼為 Polygon 格式"""
    t = ticker.upper().replace("-", "")
    # 加密貨幣不需要轉換
    if "-USD" in ticker.upper():
        return t
    return t


# ── 期權鏈（單一到期日） ──

def get_option_chain(ticker: str, expiration_date: str) -> dict:
    """
    取得指定到期日的完整期權鏈
    返回：{
        'calls': [{strike, last, iv, oi, delta, gamma, theta, vega, premium, itm}],
        'puts':  [{strike, last, iv, oi, delta, gamma, theta, vega, premium, itm}],
        'underlying_price': float,
        'expiry': str,
    }
    """
    client = get_client()
    sym = _stock_to_underlying(ticker)

    # 取得標的現價
    try:
        ticker_resp = client.get_ticker(sym)
        underlying_price = float(ticker_resp.last.price)
    except Exception:
        underlying_price = 0.0

    # 取得期權合約列表
    try:
        resp = client.list_options_contracts(
            underlying_ticker=sym,
            expiration_date=expiration_date,
            limit=500,
        )
    except Exception as e:
        log.error(f"取得期權合約列表失敗：{e}")
        return {"calls": [], "puts": [], "underlying_price": underlying_price, "expiry": expiration_date}

    calls, puts = [], []
    for c in resp:
        if not c:
            continue
        strike = c.get("strike_price", 0)
        itm = ""
        if c.get("contract_type") == "call":
            itm = "ITM" if underlying_price > strike else ("ATM" if underlying_price == strike else "OTM")
            calls.append(_build_option_entry(c, underlying_price, itm))
        elif c.get("contract_type") == "put":
            itm = "ITM" if underlying_price < strike else ("ATM" if underlying_price == strike else "OTM")
            puts.append(_build_option_entry(c, underlying_price, itm))

    calls.sort(key=lambda x: x["strike"])
    puts.sort(key=lambda x: x["strike"])

    log.info(f"{sym} {expiration_date} → {len(calls)} calls, {len(puts)} puts")
    return {
        "calls": calls,
        "puts": puts,
        "underlying_price": underlying_price,
        "expiry": expiration_date,
    }


def _build_option_entry(c: dict, underlying_price: float, itm: str) -> dict:
    """將 Polygon 回應轉換為統一格式"""
    strike = c.get("strike_price", 0)
    last_price = c.get("last_trade_price") or c.get("last") or 0
    iv = c.get("implied_volatility") or 0
    oi = c.get("open_interest") or 0
    delta = c.get("delta") or 0
    gamma = c.get("gamma") or 0
    theta = c.get("theta") or 0
    vega = c.get("vega") or 0

    # 估算權利金（內在價值 + IV 時間價值）
    intrinsic = 0
    contract_type = c.get("contract_type", "")
    if contract_type == "call":
        intrinsic = max(0, underlying_price - strike)
    elif contract_type == "put":
        intrinsic = max(0, strike - underlying_price)

    return {
        "strike": float(strike),
        "last": float(last_price),
        "iv": float(iv) * 100 if iv else 0,      # 轉為 %
        "oi": int(oi) if oi else 0,
        "delta": float(delta) if delta else 0,
        "gamma": float(gamma) if gamma else 0,
        "theta": float(theta) if theta else 0,
        "vega": float(vega) if vega else 0,
        "itm": itm,
        "intrinsic": round(intrinsic, 2),
        "premium": round(float(last_price) * 100, 2) if last_price else 0,  # 每手成本
    }


# ── 期權牆（OI Wall） ──

def build_options_wall(ticker: str, expiration_date: str, threshold_strikes: int = 10) -> dict:
    """
    構建期權牆（OI Wall）
    分析每個行權價的未平倉量（Open Interest）分佈
    找出OI最大的行權價（OI Wall）
    """
    chain = get_option_chain(ticker, expiration_date)
    underlying_price = chain.get("underlying_price", 0)

    all_oi = []
    for c in chain.get("calls", []):
        if c["oi"] > 0:
            all_oi.append({"strike": c["strike"], "oi": c["oi"], "type": "call"})
    for p in chain.get("puts", []):
        if p["oi"] > 0:
            all_oi.append({"strike": p["strike"], "oi": p["oi"], "type": "put"})

    # 找到OI牆（最大OI的行權價）
    oi_wall = max(all_oi, key=lambda x: x["oi"], default=None)

    # 按行權價分組統計
    calls_by_strike = {c["strike"]: c for c in chain.get("calls", [])}
    puts_by_strike = {p["strike"]: p for p in chain.get("puts", [])}

    all_strikes = sorted(set(list(calls_by_strike.keys()) + list(puts_by_strike.keys())))

    # 找到現價附近的行權價
    atm_strike = min(all_strikes, key=lambda s: abs(s - underlying_price)) if underlying_price and all_strikes else 0

    return {
        "ticker": ticker.upper(),
        "expiry": expiration_date,
        "underlying_price": underlying_price,
        "atm_strike": atm_strike,
        "oi_wall": oi_wall,
        "calls": chain.get("calls", []),
        "puts": chain.get("puts", []),
        "all_strikes": all_strikes,
        "total_calls_oi": sum(c["oi"] for c in chain.get("calls", [])),
        "total_puts_oi": sum(p["oi"] for p in chain.get("puts", [])),
    }
