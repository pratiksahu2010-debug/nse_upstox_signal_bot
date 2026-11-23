"""Normal NSE trend strategy: 5m EMA/VWAP/DI/ADX setup with completed 15m trend confirmation."""
import pandas as pd
from .common import Ctx, Signal, SymbolInfo, round_tick
from .config import NORMAL as C
from .indicators import Features

LONG_RSI_TRIGGER = 52.0
SHORT_RSI_TRIGGER = 42.0


def setup_side(f: Features):
    """Arm only when 5m direction and completed 15m trend direction agree."""
    htf_values = (
        getattr(f, "htf_close", None), getattr(f, "htf_ema9", None),
        getattr(f, "htf_ema21", None), getattr(f, "htf_pdi", None),
        getattr(f, "htf_mdi", None),
    )
    if any(value is None or pd.isna(value) for value in htf_values):
        return 0
    htf_bull = f.htf_close > f.htf_ema9 > f.htf_ema21 and f.htf_pdi > f.htf_mdi
    htf_bear = f.htf_close < f.htf_ema9 < f.htf_ema21 and f.htf_mdi > f.htf_pdi
    if (htf_bull and f.ema9 > f.ema21 and f.close > f.vwap
            and f.adx >= C.adx_min and f.pdi > f.mdi):
        return 1
    if (htf_bear and f.ema9 < f.ema21 and f.close < f.vwap
            and f.adx >= C.adx_min and f.mdi > f.pdi):
        return -1
    return 0


def rsi_triggered(f: Features, side: int) -> bool:
    if side > 0:
        return f.rsi_prev < LONG_RSI_TRIGGER <= f.rsi
    return f.rsi_prev > SHORT_RSI_TRIGGER >= f.rsi


def score_signal(f: Features, side: int) -> int:
    """Deterministic 0-100 quality score; no indicator threshold is modified."""
    clamp = lambda value: max(0.0, min(1.0, float(value)))
    trigger = LONG_RSI_TRIGGER if side > 0 else SHORT_RSI_TRIGGER
    htf_close = getattr(f, "htf_close", None)
    htf_ema9 = getattr(f, "htf_ema9", None)
    htf_ema21 = getattr(f, "htf_ema21", None)
    htf_di = (getattr(f, "htf_pdi", 0.0) - getattr(f, "htf_mdi", 0.0)) * side
    five_min_trend = 20.0
    fifteen_min_trend = 25.0 if (
        htf_close is not None and htf_ema9 is not None and htf_ema21 is not None
        and side * (htf_close - htf_ema9) > 0
        and side * (htf_ema9 - htf_ema21) > 0
        and htf_di > 0
    ) else 0.0
    adx_score = 15.0 * clamp((f.adx - C.adx_min) / max(1.0, 35.0 - C.adx_min))
    di_score = 10.0 * clamp((side * (f.pdi - f.mdi)) / 25.0)
    rsi_score = 10.0 * clamp((side * (f.rsi - trigger)) / 15.0)
    rvol_score = 10.0 * clamp((f.rvol - 1.0) / 2.0)
    close_quality = f.close_pos if side > 0 else 1.0 - f.close_pos
    candle_score = 10.0 * clamp(f.body_frac / 0.70) * clamp((close_quality - 0.50) / 0.50)
    return int(round(min(100.0, five_min_trend + fifteen_min_trend + adx_score
                         + di_score + rsi_score + rvol_score + candle_score)))


def evaluate(info: SymbolInfo, df: pd.DataFrame, f: Features, ctx: Ctx):
    """Create a signal only after an armed 5m setup receives its RSI cross."""
    if not C.first_signal <= f.bar_end.time() <= C.last_entry and not ctx.ignore_time:
        return None, "time_window"
    side = setup_side(f)
    if not side:
        if f.adx < C.adx_min:
            return None, "adx_min"
        if getattr(f, "htf_close", None) is None:
            return None, "htf_data_missing"
        if not ((f.ema9 > f.ema21 and f.close > f.vwap)
                or (f.ema9 < f.ema21 and f.close < f.vwap)):
            return None, "ema_vwap"
        return None, "htf_or_di_direction"
    if not rsi_triggered(f, side):
        return None, "rsi_cross"
    score = score_signal(f, side)
    # This is the sole score gate. No extra score or OI sub-gates are applied.
    if score < C.score_min:
        return None, "score_min"
    entry = f.close
    risk = C.sl_atr * f.atr
    if not (risk > 0 and pd.notna(risk)):
        return None, "invalid_atr"
    sl, tp1, tp2 = entry - side * risk, entry + side * C.tp1_r * risk, entry + side * C.tp2_r * risk
    entry, sl, tp1, tp2 = (round_tick(x, info.tick) for x in (entry, sl, tp1, tp2))
    if (side > 0 and not sl < entry < tp1 < tp2) or (side < 0 and not sl > entry > tp1 > tp2):
        return None, "invalid_levels"
    reasons = [
        "5m EMA/VWAP trend aligned",
        "15m completed trend aligned",
        f"RSI crossed {'52' if side > 0 else '42'} ({f.rsi_prev:.1f} -> {f.rsi:.1f})",
        f"ADX {f.adx:.1f} (minimum {C.adx_min:.0f})",
        f"+DI {f.pdi:.1f} / -DI {f.mdi:.1f}",
        f"Final quality score {score}/100 (minimum {C.score_min})",
        f"ATR {f.atr:.2f}; stop {C.sl_atr:.2f}xATR",
    ]
    return Signal("normal", info.symbol, "LONG" if side > 0 else "SHORT", f.bar_ts,
                  entry, sl, tp1, tp2, f.atr, score, "rsi_atr_trigger", reasons, info), None
