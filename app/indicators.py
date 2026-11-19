"""Indicators on 5-minute OHLCV frames (tz-aware IST index, ascending).

Design notes
- EMA/RSI/ADX/ATR are continuous across sessions (warm-up history), but TR and
  directional movement ignore the overnight gap (first bar of a session uses H-L,
  DM = 0) so the opening gap does not inflate ATR/ADX all day.
- VWAP resets every session.
- Flat series are handled explicitly (RSI = 50, no NaN leaking into gates).
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .config import IND


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    idx = out.index
    day = pd.Series(idx.date, index=idx)
    first = day.ne(day.shift(1)).to_numpy()
    h, l, c, o, v = out["high"], out["low"], out["close"], out["open"], out["volume"]

    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    tr = tr.where(~first, h - l)
    atr = wilder(tr, IND.atr_n)

    up = h.diff()
    dn = -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=idx).where(~first, 0.0)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=idx).where(~first, 0.0)
    atr_adx = wilder(tr, IND.adx_n)
    safe = atr_adx.replace(0, np.nan)
    pdi = 100 * wilder(pdm, IND.adx_n) / safe
    mdi = 100 * wilder(mdm, IND.adx_n) / safe
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = wilder(dx.fillna(0.0), IND.adx_n)

    delta = c.diff().fillna(0.0)
    ag = wilder(delta.clip(lower=0), IND.rsi_n)
    al = wilder((-delta).clip(lower=0), IND.rsi_n)
    rs = ag / al.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.where(~((al == 0) & (ag > 0)), 100.0)
    rsi = rsi.where(~((al == 0) & (ag == 0)), 50.0)

    dkey = day.to_numpy()
    tp = (h + l + c) / 3.0
    cum_pv = (tp * v).groupby(dkey).cumsum()
    cum_v = v.groupby(dkey).cumsum()
    fallback = tp.groupby(dkey).cumsum() / (tp.groupby(dkey).cumcount() + 1)
    vwap = (cum_pv / cum_v.replace(0, np.nan)).fillna(fallback)

    out["ema9"] = ema(c, IND.ema_fast)
    out["ema21"] = ema(c, IND.ema_slow)
    out["vwap"] = vwap
    out["rsi"] = rsi
    out["adx"] = adx
    out["pdi"] = pdi.fillna(0.0)
    out["mdi"] = mdi.fillna(0.0)
    out["atr"] = atr
    out["day"] = day
    return out


def rvol_time_of_day(df: pd.DataFrame, lookback_days: int = 5, min_days: int = 3) -> float:
    """Last-bar volume / mean volume of the same clock slot on previous sessions.
    Falls back to the 20-bar mean when there is not enough same-slot history."""
    v = df["volume"]
    ts = df.index[-1]
    slot = ts.hour * 60 + ts.minute
    same_slot = (df.index.hour * 60 + df.index.minute) == slot
    other_day = np.asarray(df.index.date != ts.date())
    prev = v[same_slot & other_day].tail(lookback_days)
    if len(prev) >= min_days and prev.mean() > 0:
        return float(v.iloc[-1] / prev.mean())
    base = v.iloc[-21:-1].mean()
    return float(v.iloc[-1] / base) if base and base > 0 else 0.0


def htf_trend(df: pd.DataFrame, minutes: int = 15) -> str:
    """'bull' / 'bear' / 'neutral' from COMPLETED 15-minute bars (EMA9 vs EMA21 and close)."""
    rule = f"{minutes}min"
    agg = df.resample(rule, closed="left", label="left").agg(
        {"close": "last", "volume": "sum"}
    )
    cnt = df["close"].resample(rule, closed="left", label="left").count()
    per_bin = minutes // 5
    # A 15m candle is complete only when all three source 5m candles exist.
    # Drop partial bins anywhere in the frame, not just a partial final bin.
    agg = agg[cnt == per_bin]
    if len(agg) < 25:
        return "neutral"
    c = agg["close"]
    e9, e21 = ema(c, IND.ema_fast), ema(c, IND.ema_slow)
    if c.iloc[-1] > e21.iloc[-1] and e9.iloc[-1] > e21.iloc[-1]:
        return "bull"
    if c.iloc[-1] < e21.iloc[-1] and e9.iloc[-1] < e21.iloc[-1]:
        return "bear"
    return "neutral"


def htf_di_context(df: pd.DataFrame, minutes: int = 15):
    """Return (+DI, -DI, ADX) from completed 15m OHLCV bars only."""
    rule = f"{minutes}min"
    agg = df.resample(rule, closed="left", label="left").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    })
    count = df["close"].resample(rule, closed="left", label="left").count()
    agg = agg[count == minutes // 5]
    if len(agg) < IND.adx_n + 1:
        return None, None, None
    htf = add_indicators(agg)
    row = htf.iloc[-1]
    values = (row["pdi"], row["mdi"], row["adx"])
    if any(pd.isna(value) for value in values):
        return None, None, None
    return tuple(float(value) for value in values)


def cross_info(df: pd.DataFrame, lookback: int = 20):
    """(crossings in last `lookback` bars, bars since latest EMA9/21 cross or None)."""
    d = (df["ema9"] - df["ema21"]).to_numpy()[-(lookback + 1):]
    s = np.sign(d)
    flips = np.nonzero(np.diff(s) != 0)[0]
    n = int(len(flips))
    age = int((len(s) - 1) - (flips[-1] + 1)) if n else None
    return n, age


@dataclass
class Features:
    bar_ts: pd.Timestamp
    bar_end: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    ema9: float
    ema21: float
    ema21_slope: float
    vwap: float
    rsi: float
    rsi_prev: float
    adx: float
    adx_prev2: float
    adx_max10: float
    pdi: float
    mdi: float
    atr: float
    atr_pct: float
    rvol: float
    crosses20: int
    cross_age: Optional[int]
    htf: str
    body_frac: float
    close_pos: float          # 0 = closed on low, 1 = closed on high
    range_atr: float
    htf_pdi: Optional[float] = None
    htf_mdi: Optional[float] = None
    htf_adx: Optional[float] = None


def compute_features(df: pd.DataFrame, candle_min: int = 5) -> Optional[Features]:
    """`df` must already contain indicator columns (add_indicators) and only CLOSED bars."""
    if len(df) < 45:
        return None
    r = df.iloc[-1]
    needed = [r.ema9, r.ema21, r.vwap, r.rsi, r.adx, r.atr, r.close]
    if any(pd.isna(x) for x in needed) or r.atr <= 0 or r.close <= 0:
        return None
    rng = float(r.high - r.low)
    body = abs(float(r.close - r.open))
    n_cross, age = cross_info(df)
    htf_pdi, htf_mdi, htf_adx = htf_di_context(df)
    ts = df.index[-1]
    return Features(
        bar_ts=ts,
        bar_end=ts + pd.Timedelta(minutes=candle_min),
        open=float(r.open), high=float(r.high), low=float(r.low), close=float(r.close),
        volume=float(r.volume),
        ema9=float(r.ema9), ema21=float(r.ema21),
        ema21_slope=float(r.ema21 - df["ema21"].iloc[-4]),
        vwap=float(r.vwap),
        rsi=float(r.rsi), rsi_prev=float(df["rsi"].iloc[-2]),
        adx=float(r.adx), adx_prev2=float(df["adx"].iloc[-3]),
        adx_max10=float(df["adx"].iloc[-10:].max()),
        pdi=float(r.pdi), mdi=float(r.mdi),
        atr=float(r.atr), atr_pct=float(r.atr / r.close * 100),
        rvol=rvol_time_of_day(df),
        crosses20=n_cross, cross_age=age,
        htf=htf_trend(df),
        body_frac=(body / rng) if rng > 0 else 0.0,
        close_pos=((float(r.close - r.low)) / rng) if rng > 0 else 0.5,
        range_atr=rng / float(r.atr),
        htf_pdi=htf_pdi, htf_mdi=htf_mdi, htf_adx=htf_adx,
    )
