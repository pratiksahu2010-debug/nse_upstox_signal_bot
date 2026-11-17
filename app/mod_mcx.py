"""Module 3 - MCX: commodity futures momentum (auto universe, up to 100 contracts).

Commodities chop more than equities, so the bar is higher than Module 1:
ADX >= 20 and not falling, Donchian-20 breakout with range expansion and time-of-day
relative volume, plus 15m trend alignment. A trend-pullback re-entry is allowed only
when ADX >= 25 (a trend that is already proven).
"""
import pandas as pd

from .common import (Ctx, Signal, SymbolInfo, adx_trend_ok, build_levels, candle_ok, pullback_resume,
                     run_gates, scale)
from .config import MCX as C
from .indicators import Features


def _in_window(f: Features, ctx: Ctx) -> bool:
    if ctx.ignore_time:
        return True
    return C.first_signal <= f.bar_end.time() <= C.last_entry


def evaluate(info: SymbolInfo, df: pd.DataFrame, f: Features, ctx: Ctx):
    if not _in_window(f, ctx):
        return None, "time_window"
    if f.ema9 > f.ema21 and f.close > f.vwap:
        s = 1
    elif f.ema9 < f.ema21 and f.close < f.vwap:
        s = -1
    else:
        return None, "trend_vwap"

    n = C.donchian_n
    prior = df.iloc[-(n + 1):-1]
    don_hi, don_lo = float(prior["high"].max()), float(prior["low"].min())
    breakout = (f.close > don_hi) if s > 0 else (f.close < don_lo)
    level = don_hi if s > 0 else don_lo
    lo, hi = C.rsi_long if s > 0 else C.rsi_short
    holder = {}

    def trigger_ok() -> bool:
        if breakout and f.range_atr >= C.expansion_min_atr:
            holder["t"] = "breakout"
            return True
        if f.adx >= C.pullback_adx_min and pullback_resume(df, f, s, C.pullback_lookback):
            holder["t"] = "pullback"
            return True
        return False

    gates = [
        ("adx_min", lambda: f.adx >= C.adx_min),
        ("di_spread", lambda: (f.pdi - f.mdi) * s >= C.di_spread_min),
        ("rsi_band", lambda: lo <= f.rsi <= hi),
        ("rsi_direction", lambda: (f.rsi - f.rsi_prev) * s >= 0),
        ("rvol", lambda: f.rvol >= C.rvol_min),
        ("atr_pct", lambda: C.atr_pct_min <= f.atr_pct <= C.atr_pct_max),
        ("ema_sep", lambda: abs(f.ema9 - f.ema21) >= C.ema_sep_min_atr * f.atr),
        ("ema_slope", lambda: f.ema21_slope * s > 0),
        ("whipsaw", lambda: f.crosses20 <= C.max_crosses_20),
        ("candle_quality", lambda: candle_ok(f, s, C.body_min, C.close_pos_min, C.range_max_atr)),
        ("extended_vwap", lambda: (f.close - f.vwap) * s <= C.ext_vwap_max_atr * f.atr),
        ("no_trigger", trigger_ok),
        ("adx_trend", lambda: adx_trend_ok(f, holder["t"], C.adx_min)),
        ("chasing", lambda: holder["t"] != "breakout" or (f.close - level) * s <= C.chase_max_atr * f.atr),
        ("htf_15m", lambda: (not C.htf_gate) or f.htf == ("bull" if s > 0 else "bear")),
    ]
    fail = run_gates(gates)
    if fail:
        return None, fail

    lv = build_levels(df, f, s, C.sl_swing_bars, C.sl_buffer_atr, C.sl_min_atr, C.sl_max_atr,
                      C.tp1_r, C.tp2_r, info.tick)
    if lv is None:
        return None, "sl_too_wide"

    trig = holder["t"]
    sc = 20 * scale(f.adx, C.adx_min, 38)
    sc += 10 * scale((f.pdi - f.mdi) * s, C.di_spread_min, 22)
    sc += 20 * scale(f.rvol, C.rvol_min, 3.0)
    sweet = (60, 70) if s > 0 else (30, 40)
    sc += 10 if sweet[0] <= f.rsi <= sweet[1] else 6
    sc += 10  # 15m aligned (hard gate)
    sc += 10 * (0.5 * scale(f.body_frac, C.body_min, 0.85)
                + 0.5 * scale(f.close_pos if s > 0 else 1 - f.close_pos, C.close_pos_min, 0.95))
    sc += 10 * scale(f.range_atr, C.expansion_min_atr, 1.8) if trig == "breakout" else 6
    # compression before expansion = cleaner breakout
    comp = float((prior["high"] - prior["low"]).tail(10).mean())
    sc += 5 if comp <= 0.9 * f.atr else 2
    sc += 5 if abs(f.close - f.vwap) <= 1.5 * f.atr else 2
    score = int(round(sc))
    if score < C.min_score:
        return None, "score"

    entry, sl, tp1, tp2 = lv
    reasons = [
        f"{'Donchian-' + str(n) + ' breakout' if trig == 'breakout' else 'trend pullback-resume'}",
        "above VWAP" if s > 0 else "below VWAP",
        f"RSI {f.rsi:.0f}",
        f"ADX {f.adx:.1f} (+DI {f.pdi:.0f} / -DI {f.mdi:.0f})",
        f"RVOL {f.rvol:.1f}x",
        f"bar {f.range_atr:.1f} ATR",
        f"15m {f.htf}",
    ]
    return Signal("mcx", info.symbol, "LONG" if s > 0 else "SHORT", f.bar_ts, entry, sl, tp1, tp2,
                  f.atr, score, trig, reasons, info), None
