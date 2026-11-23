"""Single Normal NSE trend strategy using EMA 9/21, VWAP, RSI, ADX and ATR.

All decisions are based on completed 5-minute bars. The 15-minute trend
confirmation is derived only from completed 15-minute bars and uses the same
EMA 9/21 trend structure; no additional indicators are used as signal gates.
"""
import pandas as pd

from .common import Ctx, Signal, SymbolInfo, round_tick
from .config import NORMAL as C
from .indicators import Features

LONG_RSI_TRIGGER = 52.0
SHORT_RSI_TRIGGER = 42.0


def setup_side(f: Features):
    """Return direction only when 5m and completed 15m EMA/VWAP trends agree."""
    htf_values = (
        getattr(f, "htf_close", None),
        getattr(f, "htf_ema9", None),
        getattr(f, "htf_ema21", None),
    )
    if any(value is None or pd.isna(value) for value in htf_values):
        return 0

    htf_bull = f.htf_close > f.htf_ema9 > f.htf_ema21
    htf_bear = f.htf_close < f.htf_ema9 < f.htf_ema21

    if (htf_bull and f.ema9 > f.ema21 and f.close > f.vwap
            and f.adx >= C.adx_min):
        return 1
    if (htf_bear and f.ema9 < f.ema21 and f.close < f.vwap
            and f.adx >= C.adx_min):
        return -1
    return 0


def rsi_triggered(f: Features, side: int) -> bool:
    """Require RSI to cross its directional trigger on the completed 5m bar."""
    if side > 0:
        return f.rsi_prev < LONG_RSI_TRIGGER <= f.rsi
    return f.rsi_prev > SHORT_RSI_TRIGGER >= f.rsi


def score_signal(f: Features, side: int) -> int:
    """Single 0-100 quality score using only the selected indicator set."""
    clamp = lambda value: max(0.0, min(1.0, float(value)))
    trigger = LONG_RSI_TRIGGER if side > 0 else SHORT_RSI_TRIGGER

    # Trend alignment is a hard gate in setup_side; points grade its strength.
    ema_spread_atr = side * (f.ema9 - f.ema21) / max(f.atr, 1e-9)
    vwap_distance_atr = side * (f.close - f.vwap) / max(f.atr, 1e-9)
    htf_spread_atr = side * (f.htf_ema9 - f.htf_ema21) / max(f.atr, 1e-9)

    ema_score = 25.0 + 10.0 * clamp(ema_spread_atr / 0.50)
    htf_score = 15.0 + 5.0 * clamp(htf_spread_atr / 0.50)
    vwap_score = 10.0 + 5.0 * clamp(vwap_distance_atr / 0.75)
    adx_score = 15.0 + 5.0 * clamp((f.adx - C.adx_min) / 15.0)
    rsi_score = 10.0 + 15.0 * clamp((side * (f.rsi - trigger)) / 15.0)

    return int(round(min(100.0, ema_score + htf_score + vwap_score
                         + adx_score + rsi_score)))


def evaluate(info: SymbolInfo, df: pd.DataFrame, f: Features, ctx: Ctx):
    """Create one Normal signal after trend setup and RSI confirmation."""
    if not C.first_signal <= f.bar_end.time() <= C.last_entry and not ctx.ignore_time:
        return None, "time_window"

    # ADX >= 20 is a hard floor, regardless of environment overrides.
    if f.adx < C.adx_min:
        return None, "adx_min"

    side = setup_side(f)
    if not side:
        if getattr(f, "htf_close", None) is None:
            return None, "htf_data_missing"
        if not ((f.ema9 > f.ema21 and f.close > f.vwap)
                or (f.ema9 < f.ema21 and f.close < f.vwap)):
            return None, "ema_vwap"
        return None, "completed_15m_trend"

    if not rsi_triggered(f, side):
        return None, "rsi_cross"

    score = score_signal(f, side)
    # Preserve one final score gate; no secondary score/OI/volume gates.
    if score < C.score_min:
        return None, "score_min"

    if pd.isna(f.atr) or f.atr <= 0:
        return None, "invalid_atr"

    entry = f.close
    risk = C.sl_atr * f.atr
    sl = entry - side * risk
    tp1 = entry + side * C.tp1_r * risk
    tp2 = entry + side * C.tp2_r * risk
    entry, sl, tp1, tp2 = (
        round_tick(value, info.tick) for value in (entry, sl, tp1, tp2)
    )
    if (side > 0 and not sl < entry < tp1 < tp2) or (
            side < 0 and not sl > entry > tp1 > tp2):
        return None, "invalid_levels"

    reasons = [
        "5m EMA 9/21 trend aligned",
        "Price confirmed on the correct side of session VWAP",
        "Completed 15m EMA 9/21 trend aligned",
        f"RSI crossed {LONG_RSI_TRIGGER:.0f}" if side > 0
        else f"RSI crossed below {SHORT_RSI_TRIGGER:.0f}",
        f"ADX {f.adx:.1f} (minimum {C.adx_min:.0f})",
        f"Final quality score {score}/100 (minimum {C.score_min})",
        f"ATR {f.atr:.2f}; stop {C.sl_atr:.2f}x ATR",
    ]
    return Signal(
        "normal", info.symbol, "LONG" if side > 0 else "SHORT",
        f.bar_ts, entry, sl, tp1, tp2, f.atr, score,
        "ema_vwap_rsi_atr", reasons, info,
    ), None
