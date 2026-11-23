"""Independent Momentum Explosion module using the shared completed 5m/15m feature set."""
import pandas as pd

from .common import Ctx, Signal, SymbolInfo, round_tick
from .config import MOMENTUM as C


def _trend_side(f):
    required = (
        getattr(f, "htf_close", None), getattr(f, "htf_ema9", None),
        getattr(f, "htf_ema21", None), getattr(f, "htf_pdi", None),
        getattr(f, "htf_mdi", None),
    )
    if any(value is None or pd.isna(value) for value in required):
        return 0
    bull15 = f.htf_close > f.htf_ema9 > f.htf_ema21 and f.htf_pdi > f.htf_mdi
    bear15 = f.htf_close < f.htf_ema9 < f.htf_ema21 and f.htf_mdi > f.htf_pdi
    if bull15 and f.ema9 > f.ema21 and f.close > f.vwap and f.pdi > f.mdi:
        return 1
    if bear15 and f.ema9 < f.ema21 and f.close < f.vwap and f.mdi > f.pdi:
        return -1
    return 0


def _score(f, side):
    clamp = lambda x: max(0.0, min(1.0, float(x)))
    htf_di = side * (float(f.htf_pdi) - float(f.htf_mdi))
    di = side * (float(f.pdi) - float(f.mdi))
    close_quality = f.close_pos if side > 0 else 1.0 - f.close_pos
    return int(round(
        15.0
        + (25.0 if side * (f.htf_close - f.htf_ema9) > 0
           and side * (f.htf_ema9 - f.htf_ema21) > 0 and htf_di > 0 else 0.0)
        + 15.0 * clamp((f.adx - C.adx_min) / max(1.0, 35.0 - C.adx_min))
        + 10.0 * clamp(di / 25.0)
        + 15.0 * clamp((f.rvol - 1.0) / 2.0)
        + 20.0 * clamp(f.body_frac / 0.70) * clamp((close_quality - 0.50) / 0.50)
    ))


def _path(df, f, side):
    prior20 = df.iloc[-21:-1]
    prior20_prev = df.iloc[-22:-2]
    prior6 = df.iloc[-7:-1]
    prior3 = df.iloc[-4:-1]
    prior3_prev = df.iloc[-5:-2]
    if len(prior20) < 20 or len(prior20_prev) < 20 or len(prior6) < 6 or len(prior3) < 3:
        return None
    previous_close = float(df["close"].iloc[-2])
    compression = (
        float(prior6["high"].max() - prior6["low"].min()) <= 1.5 * f.atr
        and f.rvol >= 1.3
    )
    if compression and (
        (side > 0 and previous_close <= float(prior6["high"].max()) < f.close)
        or (side < 0 and previous_close >= float(prior6["low"].min()) > f.close)
    ):
        return "COMPRESSION"
    if (
        (side > 0 and previous_close <= float(prior20_prev["high"].max()) < f.close)
        or (side < 0 and previous_close >= float(prior20_prev["low"].min()) > f.close)
    ):
        return "BREAKOUT"
    accelerating = f.adx >= C.adx_min and f.adx >= f.adx_prev2
    if accelerating and f.rvol >= 1.3:
        if (side > 0 and f.rsi >= 58 and previous_close <= float(prior3_prev["high"].max())
                and f.close > float(prior3["high"].max())):
            return "ACCELERATION"
        if (side < 0 and f.rsi <= 42 and previous_close >= float(prior3_prev["low"].min())
                and f.close < float(prior3["low"].min())):
            return "ACCELERATION"
    if (f.range_atr >= 1.5 and f.body_frac >= 0.70 and f.rvol >= 1.8
            and ((side > 0 and f.close > f.open) or (side < 0 and f.close < f.open))):
        return "EXTREME_IMPULSE"
    return None


def evaluate(info: SymbolInfo, df: pd.DataFrame, f, ctx: Ctx):
    """Emit at most one independent momentum signal for this symbol/candle."""
    if not C.first_signal <= f.bar_end.time() <= C.last_entry and not ctx.ignore_time:
        return None, "time_window"
    if f.adx < C.adx_min:
        return None, "adx_min"
    side = _trend_side(f)
    if not side:
        return None, "htf_or_5m_trend"
    path = _path(df, f, side)
    if path is None:
        return None, "no_momentum_path"
    score = _score(f, side)
    if score < C.score_min:
        return None, "score_min"
    risk = C.sl_atr * f.atr
    if not (risk > 0 and pd.notna(risk)):
        return None, "invalid_atr"
    entry = f.close
    sl = entry - side * risk
    tp1 = entry + side * C.tp1_r * risk
    tp2 = entry + side * C.tp2_r * risk
    entry, sl, tp1, tp2 = (round_tick(value, info.tick) for value in (entry, sl, tp1, tp2))
    if (side > 0 and not sl < entry < tp1 < tp2) or (side < 0 and not sl > entry > tp1 > tp2):
        return None, "invalid_levels"
    reasons = [
        f"Momentum path: {path}",
        "5m and completed 15m trend aligned",
        f"ADX {f.adx:.1f}; +DI {f.pdi:.1f}; -DI {f.mdi:.1f}",
        f"Relative volume {f.rvol:.2f}x",
        f"Final momentum score {score}/100 (minimum {C.score_min})",
        f"ATR {f.atr:.2f}; stop {C.sl_atr:.2f}xATR",
    ]
    return Signal("momentum", info.symbol, "LONG" if side > 0 else "SHORT",
                  f.bar_ts, entry, sl, tp1, tp2, f.atr, score,
                  f"MOMENTUM_EXPLOSION_{path}", reasons, info), None
