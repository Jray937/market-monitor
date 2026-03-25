#!/usr/bin/env python3
"""本地測試腳本 — options_fetcher"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=" * 60)
print("options_fetcher 本地完整測試")
print("=" * 60)

# ── 1. _to_contract_list ──
print("\n【測試 1】_to_contract_list")
from src.options_fetcher import _to_contract_list

# list → 直接返回
r = _to_contract_list([{"a": 1}])
assert r == [{"a": 1}], f"list 失敗: {r}"
print("  ✅ list → 直接返回")

# tuple → 轉 list
r = _to_contract_list(({"b": 2},))
assert r == [{"b": 2}], f"tuple 失敗: {r}"
print("  ✅ tuple → 轉 list")

# set → 轉 list（無序，確認不出錯即可）
r = _to_contract_list({1, 2, 3})
assert isinstance(r, list) and len(r) == 3, f"set 失敗: {r}"
print("  ✅ set → 轉 list")

# generator → 轉 list
def gen():
    yield {"d": 4}; yield {"e": 5}
r = _to_contract_list(gen())
assert r == [{"d": 4}, {"e": 5}], f"generator 失敗: {r}"
print("  ✅ generator → 轉 list")

# generator 耗盡後第二次調用（模擬重複迭代 Bug）
g = gen()
first = _to_contract_list(g)
second = _to_contract_list(g)
assert second == [], f"耗盡後第二次應為空: {second}"
print("  ✅ generator 耗盡後第二次 → []（安全）")

# Pro 回應對象
class ProResp:
    results = [{"f": 6}]
r = _to_contract_list(ProResp())
assert r == [{"f": 6}], f"Pro 回應失敗: {r}"
print("  ✅ Pro 回應(.results) → 取用 .results")

# 無效類型拋 TypeError
try:
    _to_contract_list(12345)
    print("  ❌ 應拋 TypeError")
except TypeError:
    print("  ✅ 無效類型拋出 TypeError")

# ── 2. _build_entry ──
print("\n【測試 2】_build_entry")
from src.options_fetcher import _build_entry

# ATM Call
e = _build_entry(
    {"strike_price": 100.0, "contract_type": "call", "last": 3.0,
     "implied_volatility": 0.25, "open_interest": 200,
     "delta": 0.5, "gamma": 0.03, "theta": -0.1, "vega": 0.2},
    underlying_price=100.0, strike=100.0)
assert e["itm"] == "ATM", f"ATM 失敗: {e['itm']}"
assert e["iv"] == 25.0, f"IV 百分比錯誤: {e['iv']}"
assert e["premium"] == 300.0, f"premium 計算錯誤: {e['premium']}"
print("  ✅ ATM Call: ITM/OTM/ATM 判定 + IV% + premium OK")

# ITM Put
e = _build_entry(
    {"strike_price": 105.0, "contract_type": "put", "last": 6.0,
     "implied_volatility": 0.30, "open_interest": 500,
     "delta": -0.4, "gamma": 0.02, "theta": -0.05, "vega": 0.15},
    underlying_price=100.0, strike=105.0)
assert e["itm"] == "ITM", f"ITM Put 失敗: {e['itm']}"
assert e["intrinsic"] == 5.0, f"ITM Put 內在價值錯誤: {e['intrinsic']}"
print("  ✅ ITM Put: 內在價值計算正確")

# OTM Call（現價 < 行權價 → OTM）
e = _build_entry(
    {"strike_price": 105.0, "contract_type": "call", "last": 2.0,
     "implied_volatility": 0.22, "open_interest": 800,
     "delta": 0.2, "gamma": 0.01, "theta": -0.02, "vega": 0.05},
    underlying_price=100.0, strike=105.0)
assert e["itm"] == "OTM", f"OTM Call 失敗: {e['itm']}"
assert e["intrinsic"] == 0.0, f"OTM Call 內在價值錯誤: {e['intrinsic']}"
print("  ✅ OTM Call: 現價 100 < 行權 105 → OTM，intrinsic=0")

# ── 3. build_options_wall (Mock) ──
print("\n【測試 3】build_options_wall (Mock 數據)")
from src.options_fetcher import build_options_wall
import src.options_fetcher

def mock_chain(ticker, expiration_date):
    return {
        "calls": [
            {"strike": 150.0, "last": 10.0, "iv": 30.0, "oi": 5000,  "delta": 0.5, "gamma": 0.02, "theta": -0.05, "vega": 0.15, "itm": "ITM"},
            {"strike": 155.0, "last": 7.0,  "iv": 28.0, "oi": 3000,  "delta": 0.4, "gamma": 0.02, "theta": -0.04, "vega": 0.12, "itm": "ITM"},
            {"strike": 160.0, "last": 4.0,  "iv": 25.0, "oi": 12000, "delta": 0.3, "gamma": 0.03, "theta": -0.03, "vega": 0.10, "itm": "ATM"},
            {"strike": 165.0, "last": 2.0,  "iv": 22.0, "oi": 8000,  "delta": 0.2, "gamma": 0.02, "theta": -0.02, "vega": 0.08, "itm": "OTM"},
            {"strike": 170.0, "last": 1.0,  "iv": 20.0, "oi": 2000,  "delta": 0.1, "gamma": 0.01, "theta": -0.01, "vega": 0.05, "itm": "OTM"},
        ],
        "puts": [
            {"strike": 150.0, "last": 3.0,  "iv": 32.0, "oi": 20000, "delta": -0.5, "gamma": 0.02, "theta": -0.04, "vega": 0.12, "itm": "ITM"},
            {"strike": 155.0, "last": 5.0,  "iv": 28.0, "oi": 15000, "delta": -0.4, "gamma": 0.02, "theta": -0.03, "vega": 0.10, "itm": "ITM"},
            {"strike": 160.0, "last": 8.0,  "iv": 26.0, "oi": 9000,  "delta": -0.3, "gamma": 0.03, "theta": -0.03, "vega": 0.09, "itm": "ATM"},
            {"strike": 165.0, "last": 12.0, "iv": 24.0, "oi": 6000,  "delta": -0.2, "gamma": 0.02, "theta": -0.02, "vega": 0.07, "itm": "OTM"},
            {"strike": 170.0, "last": 16.0, "iv": 22.0, "oi": 1000,  "delta": -0.1, "gamma": 0.01, "theta": -0.01, "vega": 0.04, "itm": "OTM"},
        ],
        "underlying_price": 160.0, "expiry": "2026-03-27",
    }

orig = src.options_fetcher.get_option_chain
src.options_fetcher.get_option_chain = mock_chain
wall = build_options_wall("AAPL", "2026-03-27")
src.options_fetcher.get_option_chain = orig

assert wall["underlying_price"] == 160.0
assert wall["atm_strike"] == 160.0, f"ATM strike: {wall['atm_strike']}"
assert wall["oi_wall"] is not None
assert wall["oi_wall"]["strike"] == 150.0, f"OI 牆 strike: {wall['oi_wall']['strike']}"
assert wall["oi_wall"]["type"] == "put", f"OI 牆 type: {wall['oi_wall']['type']}"
assert wall["total_calls_oi"] == 30000, f"Call OI: {wall['total_calls_oi']}"
assert wall["total_puts_oi"] == 51000, f"Put OI: {wall['total_puts_oi']}"
cp_ratio = wall["total_calls_oi"] / wall["total_puts_oi"]
print(f"  ✅ ATM strike = 160.0 (現價 ATM)")
print(f"  ✅ OI 牆 = $150 Put, OI=20,000 (最大)")
print(f"  ✅ 總 Call OI = 30,000 | 總 Put OI = 51,000")
print(f"  ✅ C/P 比率 = {cp_ratio:.2f}")

print("\n" + "=" * 60)
print("🎉 全部測試通過！")
print("=" * 60)
