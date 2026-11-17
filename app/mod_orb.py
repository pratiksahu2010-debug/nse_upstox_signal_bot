"""Module 2 - ORB: 100 separate F&O symbols, opening-range breakout (09:15-09:30 range).

Two entry types, both on a CLOSED 5-minute candle:
  * fresh  : first close beyond OR high/low (+ buffer) with volume expansion
  * retest : earlier valid breakout, price came back to the OR level, held, and
             closed back out with a conviction candle (better risk/reward)
Rejects: tiny/huge opening ranges, wick-heavy breakout candles, chasing, counter-gap
trades, trades against the index bias, weak ADX/volume. One signal per symbol per day.
"""
import datetime as dt

import numpy as np
import pandas as pd

from .common import Ctx, Signal, SymbolInfo, build_levels, run_gates, scale
from .config import ORB as C
from .indicators import Features


def _or_levels(df: pd.DataFrame, day):
    d = df[df["day"] == day]
    bars = d[(d.index.time >= C.or_start) & (d.index.time < C.or_end)]
    expected = int((dt.datetime.combine(day, C.or_end) - dt.datetime.combine(day, C.or_start)).seconds // 300)
    if len(bars) < expected:
        return None
    return float(bars["high"].max()), float(bars["low"].min()), bars.iloc[0]


def evaluate(info: SymbolInfo, df: pd.DataFrame, f: Features, ctx: Ctx):
    t_end = f.bar_end.time()
    if not ctx.ignore_time and not (C.first_signal <= t_end <= C.last_entry):
        return None, "time_window"
    if ctx.signaled_today(info.symbol):
        return None, "already_signaled"

    day = df.index[-1].date()
    lv = _or_levels(df, day)
    if lv is None:
        return None, "no_opening_range"
    orh, orl, first_bar = lv
    atr = f.atr
    lo_r, hi_r = C.or_range_atr
    or_range = orh - orl
    if not (lo_r * atr <= or_range <= hi_r * atr):
        return None, "or_range"

    buf = C.buffer_atr * atr
    if f.close > orh + buf:
        s, level = 1, orh
    elif f.close < orl - buf:
        s, level = -1, orl
    else:
        return None, "inside_range"

    post = df[(df["day"] == day) & (df.index.time >= C.or_end)]
    prev_close = float(df["close"].iloc[-2])
    closes_before = post["close"].iloc[:-1]
    if s > 0:
        broke_before = bool((closes_before > orh + buf).any())
        fresh = prev_close <= orh + buf
        touched = float(post["low"].tail(3).min()) <= orh + C.retest_touch_atr * atr
        held = bool((post["close"].iloc[-C.retest_window:] >= orh - 0.15 * atr).all())
    else:
        broke_before = bool((closes_before < orl - buf).any())
        fresh = prev_close >= orl - buf
        touched = float(post["high"].tail(3).max()) >= orl - C.retest_touch_atr * atr
        held = bool((post["close"].iloc[-C.retest_window:] <= orl + 0.15 * atr).all())
    retest = (not fresh) and broke_before and touched and held and (f.close > f.open if s > 0 else f.close < f.open)
    if not (fresh or retest):
        return None, "no_trigger"
    trig = "fresh" if fresh else "retest"

    # gap alignment (day open vs previous close)
    prior = df[df["day"] < day]
    gap_pct = 0.0
    if len(prior):
        gap_pct = (float(first_bar["open"]) - float(prior["close"].iloc[-1])) / float(prior["close"].iloc[-1]) * 100
    lo, hi = C.rsi_long if s > 0 else C.rsi_short
    wick_against = (f.high - max(f.open, f.close)) if s > 0 else (min(f.open, f.close) - f.low)
    rng = max(f.high - f.low, 1e-9)
    rvol_req = C.rvol_min if trig == "fresh" else C.rvol_min_retest

    def candle() -> bool:
        if f.body_frac < C.body_min or f.range_atr > C.range_max_atr:
            return False
        if wick_against / rng > C.wick_max:
            return False
        if s > 0:
            return f.close > f.open and f.close_pos >= C.close_pos_min
        return f.close < f.open and f.close_pos <= (1 - C.close_pos_min)

    gates = [
        ("adx_min", lambda: f.adx >= C.adx_min),
        ("di_spread", lambda: (f.pdi - f.mdi) * s >= C.di_spread_min),
        ("rsi_band", lambda: lo <= f.rsi <= hi),
        ("rvol", lambda: f.rvol >= rvol_req),
        ("atr_pct", lambda: C.atr_pct_min <= f.atr_pct <= C.atr_pct_max),
        ("vwap_side", lambda: (f.close - f.vwap) * s > 0),
        ("ema_side", lambda: (f.close - f.ema9) * s > 0 and (f.close - f.ema21) * s > 0),
        ("candle_quality", candle),
        ("chasing", lambda: (f.close - level) * s <= C.chase_max_atr * atr),
        ("counter_gap", lambda: not (gap_pct * s < -C.counter_gap_pct)),
        ("market_bias", lambda: (not C.bias_gate) or ctx.bias != ("bear" if s > 0 else "bull")),
    ]
    fail = run_gates(gates)
    if fail:
        return None, fail

    hard = (orl - C.sl_buffer_atr * atr) if s > 0 else (orh + C.sl_buffer_atr * atr)
    lv2 = build_levels(df, f, s, C.sl_swing_bars, C.sl_buffer_atr, C.sl_min_atr, C.sl_max_atr,
                       C.tp1_r, C.tp2_r, info.tick, hard_stop=hard)
    if lv2 is None:
        return None, "sl_too_wide"

    # ---- score ----
    sc = 20 * scale(f.rvol, rvol_req, 3.0)
    sc += 15 * scale(f.adx, C.adx_min, 35)
    sweet = (62, 72) if s > 0 else (28, 38)
    sc += 10 if sweet[0] <= f.rsi <= sweet[1] else 6
    sc += 10 if 1.5 * atr <= or_range <= 3.5 * atr else 5
    sc += 10 if gap_pct * s >= 0.3 else (5 if abs(gap_pct) < 0.3 else 0)
    open_drive = (abs(float(first_bar["open"]) - float(first_bar["low"])) <= 0.05 * atr) if s > 0 else \
                 (abs(float(first_bar["open"]) - float(first_bar["high"])) <= 0.05 * atr)
    sc += 10 if open_drive else 0
    sc += 5 if abs(f.close - f.vwap) <= 1.0 * atr else 2
    sc += 10 if f.htf == ("bull" if s > 0 else "bear") else (5 if f.htf == "neutral" else 0)
    sc += 5 if ctx.bias == ("bull" if s > 0 else "bear") else 2
    sc += 5 if f.bar_end.time() <= dt.time(10, 15) else 2
    sc += 5 if trig == "retest" else 3
    score = int(round(sc))
    if score < C.min_score:
        return None, "score"

    entry, sl, tp1, tp2 = lv2
    reasons = [
        f"OR {orl:.2f}-{orh:.2f} ({or_range / atr:.1f} ATR)",
        f"{'breakout' if trig == 'fresh' else 'retest hold'} {'above ORH' if s > 0 else 'below ORL'}",
        "above VWAP" if s > 0 else "below VWAP",
        f"RSI {f.rsi:.0f}",
        f"ADX {f.adx:.1f} (+DI {f.pdi:.0f} / -DI {f.mdi:.0f})",
        f"RVOL {f.rvol:.1f}x",
        f"gap {gap_pct:+.2f}%" + (" | open=" + ("low" if s > 0 else "high") if open_drive else ""),
        f"15m {f.htf}",
    ]
    if ctx.bias != "neutral":
        reasons.append(f"NIFTY {ctx.bias}")
    return Signal("orb", info.symbol, "LONG" if s > 0 else "SHORT", f.bar_ts, entry, sl, tp1, tp2,
                  f.atr, score, trig, reasons, info), None
