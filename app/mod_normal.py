"""Single CALL/PUT trend strategy for NSE midcap stocks and index derivatives."""
import pandas as pd

from .common import Ctx, Signal, SymbolInfo, round_tick
from .config import NORMAL as C
from .indicators import Features


def evaluate(info: SymbolInfo, df: pd.DataFrame, f: Features, ctx: Ctx):
    """Evaluate one fully closed 5m candle; no score or secondary strategy gates."""
    if not C.first_signal <= f.bar_end.time() <= C.last_entry and not ctx.ignore_time:
        return None, "time_window"

    if f.ema9 > f.ema21 and f.close > f.vwap:
        side = 1
    elif f.ema9 < f.ema21 and f.close < f.vwap:
        side = -1
    else:
        return None, "ema_vwap"

    if f.adx < C.adx_min:
        return None, "adx_min"
    if side > 0:
        if f.rsi <= 50:
            return None, "rsi_direction"
        if f.pdi <= f.mdi:
            return None, "di_direction"
        expected_htf = "bull"
    else:
        if f.rsi >= 50:
            return None, "rsi_direction"
        if f.mdi <= f.pdi:
            return None, "di_direction"
        expected_htf = "bear"

    if f.htf != expected_htf:
        return None, "htf_15m"

    # Fixed ATR risk model. Stops and targets are calculated from the signal
    # candle close; all values are rounded to the instrument tick.
    entry = f.close
    risk = C.sl_atr * f.atr
    if not (risk > 0 and pd.notna(risk)):
        return None, "invalid_atr"
    sl = entry - side * risk
    tp1 = entry + side * C.tp1_r * risk
    tp2 = entry + side * C.tp2_r * risk
    entry, sl, tp1, tp2 = (
        round_tick(x, info.tick) for x in (entry, sl, tp1, tp2)
    )
    if (side > 0 and not sl < entry < tp1 < tp2) or (side < 0 and not sl > entry > tp1 > tp2):
        return None, "invalid_levels"

    reasons = [
        "EMA9 above EMA21" if side > 0 else "EMA9 below EMA21",
        "Price above VWAP" if side > 0 else "Price below VWAP",
        f"RSI {f.rsi:.1f}",
        f"ADX {f.adx:.1f} (minimum {C.adx_min:.0f})",
        f"+DI {f.pdi:.1f} / -DI {f.mdi:.1f}",
        f"15m context {f.htf}",
        f"ATR {f.atr:.2f}",
    ]
    return Signal(
        "normal", info.symbol, "LONG" if side > 0 else "SHORT",
        f.bar_ts, entry, sl, tp1, tp2, f.atr, 0, "ema_vwap",
        reasons, info,
    ), None
