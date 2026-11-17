"""Module 1 - NORMAL: 50 liquid F&O stocks + indices, 5-minute trend signals.

Philosophy: trade only when trend, momentum, strength, participation and candle
quality ALL agree, and never chase. Two entry triggers:
  * fresh EMA9/21 cross (<= N bars old) that already has VWAP + ADX support, or
  * trend pullback into the EMA zone that re-accelerates on the signal bar.
Every gate is evaluated in order; the first failing gate is returned as the reject
reason so /diag can show exactly why nothing fired.
"""
from typing import Optional

import pandas as pd

from .common import (Ctx, Signal, SymbolInfo, adx_trend_ok, build_levels, candle_ok,
                     pullback_resume, run_gates, scale)
from .config import NORMAL as C
from .indicators import Features


def _in_window(f: Features, ctx: Ctx) -> bool:
    if ctx.ignore_time:
        return True
    t = f.bar_end.time()
    return C.first_signal <= t <= C.last_entry


def _score(f: Features, s: int, trig: str, bias: str, is_index: bool) -> tuple[int, list]:
    parts = []
    sc = 0.0
    a = 20 * scale(f.adx, C.adx_min, 35)
    sc += a
    sc += 10 * scale((f.pdi - f.mdi) * s, C.di_spread_min, 20)
    sc += 15 * scale(f.rvol, C.rvol_min, 2.5)
    lo, hi = C.rsi_long if s > 0 else C.rsi_short
    sweet = (58, 66) if s > 0 else (34, 42)
    if sweet[0] <= f.rsi <= sweet[1]:
        sc += 10
    elif lo <= f.rsi <= hi:
        sc += 6
    sc += 10  # 15m alignment is a hard gate; reaching here earns the points
    if not is_index:
        sc += 8 if ((bias == "bull" and s > 0) or (bias == "bear" and s < 0)) else 4
    else:
        sc += 6
    sc += 10 * (0.5 * scale(f.body_frac, C.body_min, 0.85)
                + 0.5 * scale(f.close_pos if s > 0 else 1 - f.close_pos, C.close_pos_min, 0.95))
    sc += 10 if trig == "pullback" else 7
    if abs(f.close - f.vwap) <= 0.8 * f.atr:
        sc += 5
    t = f.bar_end.time().strftime("%H:%M")
    if "09:45" <= t <= "11:30" or "13:45" <= t <= "14:30":
        sc += 5
    return int(round(sc)), parts


def evaluate(info: SymbolInfo, df: pd.DataFrame, f: Features, ctx: Ctx):
    """Return (Signal | None, reject_reason | None). `df` = closed bars with indicators."""
    if not _in_window(f, ctx):
        return None, "time_window"

    if f.ema9 > f.ema21 and f.close > f.vwap:
        s = 1
    elif f.ema9 < f.ema21 and f.close < f.vwap:
        s = -1
    else:
        return None, "trend_vwap"

    in_chop = (not ctx.ignore_time) and C.chop_start <= f.bar_end.time() <= C.chop_end
    adx_req = max(C.adx_min, C.chop_adx_min) if in_chop else C.adx_min
    rvol_req = max(C.rvol_min, C.chop_rvol_min) if in_chop else C.rvol_min
    lo, hi = C.rsi_long if s > 0 else C.rsi_short

    cross_trigger = f.cross_age is not None and f.cross_age <= C.cross_max_age
    trig_holder = {}

    def trigger_ok() -> bool:
        if cross_trigger:
            trig_holder["t"] = "cross"
            return True
        if pullback_resume(df, f, s, C.pullback_lookback):
            trig_holder["t"] = "pullback"
            return True
        return False

    gates = [
        ("adx_min", lambda: f.adx >= adx_req),
        ("di_spread", lambda: (f.pdi - f.mdi) * s >= C.di_spread_min),
        ("rsi_band", lambda: lo <= f.rsi <= hi),
        ("rsi_direction", lambda: (f.rsi - f.rsi_prev) * s >= 0),
        ("rvol", lambda: f.rvol >= rvol_req),
        ("atr_pct", lambda: C.atr_pct_min <= f.atr_pct <= C.atr_pct_max),
        ("ema_sep", lambda: abs(f.ema9 - f.ema21) >= C.ema_sep_min_atr * f.atr),
        ("ema_slope", lambda: f.ema21_slope * s > 0),
        ("whipsaw", lambda: f.crosses20 <= C.max_crosses_20),
        ("candle_quality", lambda: candle_ok(f, s, C.body_min, C.close_pos_min, C.range_max_atr)),
        ("extended_vwap", lambda: (f.close - f.vwap) * s <= C.ext_vwap_max_atr * f.atr),
        ("extended_ema9", lambda: abs(f.close - f.ema9) <= C.ext_ema9_max_atr * f.atr),
        ("no_trigger", trigger_ok),
        ("adx_trend", lambda: adx_trend_ok(f, trig_holder["t"], adx_req)),
        ("htf_15m", lambda: (not C.htf_gate) or f.htf == ("bull" if s > 0 else "bear")),
        ("market_bias", lambda: (not C.bias_gate) or info.is_index
            or ctx.bias != ("bear" if s > 0 else "bull")),
    ]
    fail = run_gates(gates)
    if fail:
        return None, fail

    lv = build_levels(df, f, s, C.sl_swing_bars, C.sl_buffer_atr, C.sl_min_atr, C.sl_max_atr,
                      C.tp1_r, C.tp2_r, info.tick)
    if lv is None:
        return None, "sl_too_wide"
    score, _ = _score(f, s, trig_holder["t"], ctx.bias, info.is_index)
    if score < C.min_score:
        return None, "score"

    entry, sl, tp1, tp2 = lv
    trig = trig_holder["t"]
    reasons = [
        ("EMA9>EMA21" if s > 0 else "EMA9<EMA21") + (f" fresh cross ({f.cross_age} bars)" if trig == "cross" else " pullback-resume"),
        "above VWAP" if s > 0 else "below VWAP",
        f"RSI {f.rsi:.0f}",
        f"ADX {f.adx:.1f} (+DI {f.pdi:.0f} / -DI {f.mdi:.0f})",
        f"RVOL {f.rvol:.1f}x",
        f"15m {f.htf}",
        f"ATR {f.atr:.2f} ({f.atr_pct:.2f}%)",
    ]
    if not info.is_index and ctx.bias != "neutral":
        reasons.append(f"NIFTY {ctx.bias}")
    return Signal("normal", info.symbol, "LONG" if s > 0 else "SHORT", f.bar_ts, entry, sl, tp1, tp2,
                  f.atr, score, trig, reasons, info), None
