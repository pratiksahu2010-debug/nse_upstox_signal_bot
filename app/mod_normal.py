"""Normal NSE trend strategy: 5m EMA/VWAP/DI/ADX setup with RSI-cross trigger."""
import pandas as pd
from .common import Ctx, Signal, SymbolInfo, round_tick
from .config import NORMAL as C
from .indicators import Features

LONG_RSI_TRIGGER = 52.0
SHORT_RSI_TRIGGER = 42.0


def setup_side(f: Features):
    """Cheap directional gates used to arm a symbol; no 15m dependency."""
    if f.ema9 > f.ema21 and f.close > f.vwap and f.adx >= C.adx_min and f.pdi > f.mdi:
        return 1
    if f.ema9 < f.ema21 and f.close < f.vwap and f.adx >= C.adx_min and f.mdi > f.pdi:
        return -1
    return 0


def rsi_triggered(f: Features, side: int) -> bool:
    if side > 0:
        return f.rsi_prev < LONG_RSI_TRIGGER <= f.rsi
    return f.rsi_prev > SHORT_RSI_TRIGGER >= f.rsi


def evaluate(info: SymbolInfo, df: pd.DataFrame, f: Features, ctx: Ctx):
    """Create a signal only after an armed setup receives its RSI cross."""
    if not C.first_signal <= f.bar_end.time() <= C.last_entry and not ctx.ignore_time:
        return None, "time_window"
    side = setup_side(f)
    if not side:
        if f.adx < C.adx_min: return None, "adx_min"
        if not ((f.ema9 > f.ema21 and f.close > f.vwap) or (f.ema9 < f.ema21 and f.close < f.vwap)):
            return None, "ema_vwap"
        return None, "di_direction"
    if not rsi_triggered(f, side):
        return None, "rsi_cross"
    entry = f.close
    risk = C.sl_atr * f.atr
    if not (risk > 0 and pd.notna(risk)):
        return None, "invalid_atr"
    sl, tp1, tp2 = entry - side * risk, entry + side * C.tp1_r * risk, entry + side * C.tp2_r * risk
    entry, sl, tp1, tp2 = (round_tick(x, info.tick) for x in (entry, sl, tp1, tp2))
    if (side > 0 and not sl < entry < tp1 < tp2) or (side < 0 and not sl > entry > tp1 > tp2):
        return None, "invalid_levels"
    reasons = [
        "EMA9 above EMA21" if side > 0 else "EMA9 below EMA21",
        "Price above VWAP" if side > 0 else "Price below VWAP",
        f"RSI crossed {'52' if side > 0 else '42'} ({f.rsi_prev:.1f} -> {f.rsi:.1f})",
        f"ADX {f.adx:.1f} (minimum {C.adx_min:.0f})",
        f"+DI {f.pdi:.1f} / -DI {f.mdi:.1f}",
        f"ATR {f.atr:.2f}; stop {C.sl_atr:.2f}xATR",
    ]
    return Signal("normal", info.symbol, "LONG" if side > 0 else "SHORT", f.bar_ts,
                  entry, sl, tp1, tp2, f.atr, 0, "rsi_atr_trigger", reasons, info), None
