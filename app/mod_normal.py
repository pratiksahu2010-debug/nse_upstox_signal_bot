"""Normal-only NSE F&O strategy using completed 5-minute EMA 9/21, VWAP, RSI, ADX and ATR.

No higher-timeframe candles or filters are used. All entry decisions are made
from completed 5-minute bars; options remain recommendations only.
"""
import math

import pandas as pd

from .common import Ctx, Signal, SymbolInfo, round_tick
from .config import NORMAL as C
from .indicators import Features

LONG_RSI_TRIGGER = 52.0
SHORT_RSI_TRIGGER = 42.0


def _tick_round_directional(value: float, tick: float, direction: str) -> float:
    """Round a stop/target away from entry so tick rounding cannot weaken its distance."""
    if not tick or tick <= 0:
        return round(value, 2)
    units = value / tick
    # Small tolerance prevents binary floating-point noise at exact tick values.
    if direction == "down":
        return round(math.floor(units + 1e-10) * tick, 6)
    return round(math.ceil(units - 1e-10) * tick, 6)


def setup_side(f: Features):
    """Return direction from completed 5m EMA/VWAP alignment and ADX floor."""
    if pd.isna(f.adx) or f.adx < C.adx_min:
        return 0
    if f.ema9 > f.ema21 and f.close > f.vwap:
        return 1
    if f.ema9 < f.ema21 and f.close < f.vwap:
        return -1
    return 0


def rsi_triggered(f: Features, side: int) -> bool:
    """Require RSI to cross its directional trigger on the completed 5m bar."""
    if side > 0:
        return f.rsi_prev < LONG_RSI_TRIGGER <= f.rsi
    return f.rsi_prev > SHORT_RSI_TRIGGER >= f.rsi


def score_signal(f: Features, side: int) -> int:
    """Single 0-100 score using only 5m EMA spread/slope, VWAP, ADX and RSI."""
    clamp = lambda value: max(0.0, min(1.0, float(value)))
    trigger = LONG_RSI_TRIGGER if side > 0 else SHORT_RSI_TRIGGER

    ema_spread_atr = side * (f.ema9 - f.ema21) / max(f.atr, 1e-9)
    ema_slope_atr = side * f.ema21_slope / max(f.atr, 1e-9)
    vwap_distance_atr = side * (f.close - f.vwap) / max(f.atr, 1e-9)

    # Keep the same 100-point component weights and final score gate while
    # replacing the removed 15m trend component with 5m EMA21 slope strength.
    ema_score = 20.0 + 10.0 * clamp(ema_spread_atr)
    slope_score = 15.0 + 5.0 * clamp(ema_slope_atr)
    vwap_score = 10.0 + 5.0 * clamp(vwap_distance_atr / 1.5)
    adx_score = 10.0 + 10.0 * clamp((f.adx - C.adx_min) / 25.0)
    rsi_score = 5.0 + 10.0 * clamp((side * (f.rsi - trigger)) / 20.0)

    return int(round(min(100.0, ema_score + slope_score + vwap_score
                         + adx_score + rsi_score)))


def evaluate(info: SymbolInfo, df: pd.DataFrame, f: Features, ctx: Ctx):
    """Create one Normal signal after 5m trend setup and RSI confirmation."""
    if not C.first_signal <= f.bar_end.time() <= C.last_entry and not ctx.ignore_time:
        return None, "time_window"

    if pd.isna(f.adx) or f.adx < C.adx_min:
        return None, "adx_min"

    side = setup_side(f)
    if not side:
        return None, "ema_vwap"

    if not rsi_triggered(f, side):
        return None, "rsi_cross"

    score = score_signal(f, side)
    # Preserve one final score gate; no secondary score/OI/volume gates.
    if score < C.score_min:
        return None, "score_min"

    if pd.isna(f.atr) or f.atr <= 0:
        return None, "invalid_atr"
    if (not math.isfinite(C.sl_atr) or C.sl_atr <= 0
            or not math.isfinite(C.tp1_r) or C.tp1_r <= 0
            or not math.isfinite(C.tp2_r) or C.tp2_r <= C.tp1_r):
        return None, "invalid_risk_config"

    # Round entry to the nearest tradable tick. Then derive targets from the
    # actual tick-rounded stop distance, not the pre-rounded ATR distance.
    entry = round_tick(float(f.close), info.tick)
    raw_risk = C.sl_atr * float(f.atr)
    if side > 0:
        sl = _tick_round_directional(entry - raw_risk, info.tick, "down")
    else:
        sl = _tick_round_directional(entry + raw_risk, info.tick, "up")

    risk = abs(entry - sl)
    if not math.isfinite(risk) or risk <= 0:
        return None, "invalid_levels"

    raw_tp1 = entry + side * C.tp1_r * risk
    raw_tp2 = entry + side * C.tp2_r * risk
    if side > 0:
        tp1 = _tick_round_directional(raw_tp1, info.tick, "up")
        tp2 = _tick_round_directional(raw_tp2, info.tick, "up")
    else:
        tp1 = _tick_round_directional(raw_tp1, info.tick, "down")
        tp2 = _tick_round_directional(raw_tp2, info.tick, "down")

    if (side > 0 and not sl < entry < tp1 < tp2) or (
            side < 0 and not sl > entry > tp1 > tp2):
        return None, "invalid_levels"

    reasons = [
        "Completed 5m EMA 9/21 trend aligned",
        "Price confirmed on the correct side of session VWAP",
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
