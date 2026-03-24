"""
技術分析引擎
使用 pandas-ta 計算技術指標
"""
import pandas as pd

from dataclasses import dataclass
from typing import Literal
from .logger import setup_logger

log = setup_logger("analyzer")


@dataclass
class TAResult:
    symbol: str
    current_price: float
    prev_close: float
    pct_change: float
    # 均線
    sma20: float | None
    sma50: float | None
    sma200: float | None
    ema12: float | None
    ema26: float | None
    # 動量
    rsi14: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    # 波動
    bb_upper: float | None
    bb_middle: float | None
    bb_lower: float | None
    # 狀態
    above_ma200: bool | None
    below_ma200: bool | None


def compute_ta(symbol: str, df: pd.DataFrame) -> TAResult | None:
    """對 DataFrame 計算完整技術指標"""
    if df.empty or len(df) < 60:
        log.warning(f"{symbol} 數據不足（<60根K線），跳過分析")
        return None

    close = df["Close"]
    current_price = float(close.iloc[-1])
    prev_close = float(close.iloc[-2]) if len(close) >= 2 else current_price
    pct_change = ((current_price - prev_close) / prev_close * 100) if prev_close else 0.0

    # --- 移動平均線 ---
    sma20 = float(close.iloc[-20:].mean()) if len(close) >= 20 else None
    sma50 = float(close.iloc[-50:].mean()) if len(close) >= 50 else None
    sma200 = float(close.iloc[-200:].mean()) if len(close) >= 200 else None
    ema12 = float(close.ewm(span=12, adjust=False).mean().iloc[-1]) if len(close) >= 12 else None
    ema26 = float(close.ewm(span=26, adjust=False).mean().iloc[-1]) if len(close) >= 26 else None

    # --- MACD ---
    try:
        macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        macd_signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd_val = float(macd_line.iloc[-1])
        macd_sig = float(macd_signal_line.iloc[-1])
        macd_hist = float(macd_line.iloc[-1] - macd_signal_line.iloc[-1])
        macd_hist_prev = float(macd_line.iloc[-2] - macd_signal_line.iloc[-2])
    except Exception:
        macd_val = macd_sig = macd_hist = macd_hist_prev = None

    # --- RSI(14) ---
    try:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi14 = float((100 - 100 / (1 + rs)).iloc[-1]) if not loss.iloc[-1] == 0 else None
    except Exception:
        rsi14 = None

    # --- 布林帶 (20,2) ---
    try:
        bb_std = close.rolling(20).std()
        bb_mid = close.rolling(20).mean()
        bb_upper = float((bb_mid + 2 * bb_std).iloc[-1])
        bb_middle = float(bb_mid.iloc[-1])
        bb_lower = float((bb_mid - 2 * bb_std).iloc[-1])
    except Exception:
        bb_upper = bb_middle = bb_lower = None

    # --- MA200 狀態 ---
    above_ma200 = (current_price > sma200) if sma200 else None
    below_ma200 = (current_price < sma200) if sma200 else None

    return TAResult(
        symbol=symbol,
        current_price=current_price,
        prev_close=prev_close,
        pct_change=pct_change,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        ema12=ema12,
        ema26=ema26,
        rsi14=rsi14,
        macd=macd_val,
        macd_signal=macd_sig,
        macd_hist=macd_hist,
        bb_upper=bb_upper,
        bb_middle=bb_middle,
        bb_lower=bb_lower,
        above_ma200=above_ma200,
        below_ma200=below_ma200,
    )


def check_alert(ta: TAResult, alert_type: str, threshold: float = 0) -> tuple[bool, str]:
    """
    檢查是否觸發警報
    回傳 (是否觸發, 描述文字)
    """
    price_str = f"${ta.current_price:.2f}" if ta.current_price < 1000 else f"${ta.current_price:.2f}"
    pct = f"{ta.pct_change:+.2f}%"
    label = f"[{ta.symbol}]"

    try:
        if alert_type == "rsi_overbought":
            triggered = ta.rsi14 is not None and ta.rsi14 > threshold
            msg = (
                f"{label} 📈 RSI 超買\n"
                f"價格：{price_str} ({pct})\n"
                f"RSI(14)：{ta.rsi14:.1f} > {threshold} 【超買】\n"
                f"建議：注意獲利了結"
            )

        elif alert_type == "rsi_oversold":
            triggered = ta.rsi14 is not None and ta.rsi14 < threshold
            msg = (
                f"{label} 📉 RSI 超賣\n"
                f"價格：{price_str} ({pct})\n"
                f"RSI(14)：{ta.rsi14:.1f} < {threshold} 【超賣】\n"
                f"建議：關注反彈機會"
            )

        elif alert_type == "macd_cross_up":
            if ta.macd is not None and ta.macd_signal is not None:
                hist = ta.macd_hist
                # 簡化：MACD 在零軸以上且 RSI 中性
                triggered = ta.macd > ta.macd_signal and ta.rsi14 is not None and 40 < ta.rsi14 < 60
                msg = (
                    f"{label} ✅ MACD 金叉\n"
                    f"價格：{price_str} ({pct})\n"
                    f"MACD：{ta.macd:.4f} > 信號線：{ta.macd_signal:.4f}\n"
                    f"RSI(14)：{ta.rsi14:.1f}"
                )
            else:
                triggered, msg = False, ""

        elif alert_type == "macd_cross_down":
            if ta.macd is not None and ta.macd_signal is not None:
                triggered = ta.macd < ta.macd_signal and ta.rsi14 is not None and 40 < ta.rsi14 < 60
                msg = (
                    f"{label} 🔴 MACD 死叉\n"
                    f"價格：{price_str} ({pct})\n"
                    f"MACD：{ta.macd:.4f} < 信號線：{ta.macd_signal:.4f}\n"
                    f"RSI(14)：{ta.rsi14:.1f}"
                )
            else:
                triggered, msg = False, ""

        elif alert_type == "price_cross_ma200":
            if ta.above_ma200 is not None:
                triggered = ta.above_ma200
                ma_val = ta.sma200
                direction = "🚀 突破 MA200（多頭訊號）" if triggered else "📉 跌破 MA200（空頭訊號）"
                msg = (
                    f"{label} {direction}\n"
                    f"價格：{price_str} ({pct})\n"
                    f"MA200：${ma_val:.2f}\n"
                    f"{'✅ 價格高於均線' if triggered else '⚠️ 價格低於均線'}"
                )
            else:
                triggered, msg = False, ""

        elif alert_type == "bollinger_upper":
            triggered = ta.bb_upper is not None and ta.current_price >= ta.bb_upper
            msg = (
                f"{label} 💥 觸碰布林上軌\n"
                f"價格：{price_str} ({pct})\n"
                f"布林上軌：${ta.bb_upper:.2f}\n"
                f"提示：強勢突破，波動放大"
            )

        elif alert_type == "bollinger_lower":
            triggered = ta.bb_lower is not None and ta.current_price <= ta.bb_lower
            msg = (
                f"{label} 📍 觸碰布林下軌\n"
                f"價格：{price_str} ({pct})\n"
                f"布林下軌：${ta.bb_lower:.2f}\n"
                f"提示：弱勢關注支撐"
            )

        else:
            log.warning(f"未知的警報類型：{alert_type}")
            return False, ""

        return triggered, msg

    except Exception as e:
        log.error(f"警報檢查失敗 {ta.symbol} {alert_type}：{e}")
        return False, ""
