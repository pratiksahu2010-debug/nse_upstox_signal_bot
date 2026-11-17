"""Shared types and helpers for the three signal modules."""
from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

from .indicators import Features


@dataclass
class SymbolInfo:
    symbol: str                     # display name, e.g. RELIANCE, NIFTY, CRUDEOIL
    key: str                        # Upstox instrument_key used for candles
    module: str                     # normal | orb | mcx
    lot: int = 0
    tick: float = 0.05
    is_index: bool = False
    expiry: str = ""                # for futures-based symbols (indices, MCX)
    underlying: str = ""            # option underlying for NSE F&O


@dataclass
class Signal:
    module: str
    symbol: str
    side: str                       # LONG | SHORT
    bar_ts: pd.Timestamp            # start of the signal candle
    entry: float
    sl: float
    tp1: float
    tp2: float
    atr: float
    score: int
    trigger: str
    reasons: list = field(default_factory=list)
    info: Optional[SymbolInfo] = None
    option: Optional[dict] = None   # filled by engine for NSE modules
    forced: bool = False

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)


@dataclass
class Ctx:
    """Per-evaluation context handed to a module."""
    bias: str = "neutral"                       # index-future bias: bull | bear | neutral
    ignore_time: bool = False                   # /force testing: skip clock windows
    signaled_today: Callable[[str], bool] = lambda sym: False


def round_tick(x: float, tick: float) -> float:
    if not tick or tick <= 0:
        return round(x, 2)
    return round(round(x / tick) * tick, 6)


def run_gates(gates: list[tuple[str, Callable[[], bool]]]) -> Optional[str]:
    """Evaluate gates lazily in order; return the name of the first failing gate."""
    for name, fn in gates:
        if not fn():
            return name
    return None


def candle_ok(f: Features, s: int, body_min: float, pos_min: float, range_max_atr: float) -> bool:
    """Conviction candle in the trade direction, but not an exhaustion bar."""
    if f.body_frac < body_min or f.range_atr > range_max_atr:
        return False
    if s > 0:
        return f.close > f.open and f.close_pos >= pos_min
    return f.close < f.open and f.close_pos <= (1 - pos_min)


def build_levels(df: pd.DataFrame, f: Features, s: int, swing_bars: int, buffer_atr: float,
                 sl_min_atr: float, sl_max_atr: float, tp1_r: float, tp2_r: float,
                 tick: float, hard_stop: Optional[float] = None):
    """Swing-based stop. Too-tight stops are widened to sl_min_atr; too-wide stops are
    rejected (returns None) because widening them destroys the risk/reward."""
    entry = f.close
    if s > 0:
        sl = float(df["low"].iloc[-swing_bars:].min()) - buffer_atr * f.atr
        if hard_stop is not None:
            sl = max(sl, hard_stop)
        risk = entry - sl
    else:
        sl = float(df["high"].iloc[-swing_bars:].max()) + buffer_atr * f.atr
        if hard_stop is not None:
            sl = min(sl, hard_stop)
        risk = sl - entry
    if risk < sl_min_atr * f.atr:
        risk = sl_min_atr * f.atr
        sl = entry - s * risk
    if risk > sl_max_atr * f.atr:
        return None
    tp1 = entry + s * tp1_r * risk
    tp2 = entry + s * tp2_r * risk
    return (round_tick(entry, tick), round_tick(sl, tick),
            round_tick(tp1, tick), round_tick(tp2, tick))


def scale(x: float, lo: float, hi: float) -> float:
    """Linear 0..1 between lo and hi."""
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def pullback_resume(df: pd.DataFrame, f: Features, s: int, lookback: int) -> bool:
    """Trend pullback that held the EMA zone and re-accelerated on the signal bar."""
    w = df.iloc[-(lookback + 1):-1]
    if len(w) < 3:
        return False
    prev = df.iloc[-2]
    if s > 0:
        touched = ((w["low"] <= w["ema9"]) & (w["low"] >= w["ema21"] - 0.3 * f.atr)).any()
        return bool(touched and f.close > f.ema9 and f.close > prev.high and f.close > f.open)
    touched = ((w["high"] >= w["ema9"]) & (w["high"] <= w["ema21"] + 0.3 * f.atr)).any()
    return bool(touched and f.close < f.ema9 and f.close < prev.low and f.close < f.open)


def market_bias(f: Optional[Features]) -> str:
    """Bias of the index future: price vs VWAP and EMA stack must agree."""
    if f is None:
        return "neutral"
    if f.close > f.vwap and f.ema9 > f.ema21:
        return "bull"
    if f.close < f.vwap and f.ema9 < f.ema21:
        return "bear"
    return "neutral"


def adx_trend_ok(f: Features, trig: str, adx_min: float) -> bool:
    """Trigger-aware ADX behaviour.
    cross / breakout : trend is starting -> ADX must not be falling.
    pullback         : ADX naturally dips during the pullback -> accept as long as it is still
                       above the minimum and has not lost more than 15 % of its recent peak."""
    if trig == "pullback":
        return f.adx >= adx_min and f.adx >= 0.85 * f.adx_max10
    return f.adx >= f.adx_prev2
