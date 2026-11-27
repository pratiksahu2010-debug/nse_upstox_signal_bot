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
    """Last-bar volume / same clock-slot volume from prior sessions.

    Uses a 20-bar fallback when there is insufficient same-slot history. Bad or
    missing volume is treated as unavailable (0.0), never as NaN/inf.
    """
    if df.empty or "volume" not in df or not isinstance(df.index, pd.DatetimeIndex):
        return 0.0
    v = pd.to_numeric(df["volume"], errors="coerce")
    current = float(v.iloc[-1]) if pd.notna(v.iloc[-1]) else float("nan")
    if not np.isfinite(current) or current < 0:
        return 0.0

    ts = df.index[-1]
    slot = ts.hour * 60 + ts.minute
    same_slot = (df.index.hour * 60 + df.index.minute) == slot
    other_day = np.asarray(df.index.date != ts.date())
    prev = v[same_slot & other_day].tail(max(1, int(lookback_days)))
    prev = prev[np.isfinite(prev.to_numpy(dtype=float)) & (prev >= 0)]
    if len(prev) >= max(1, int(min_days)):
        baseline = float(prev.mean())
        if np.isfinite(baseline) and baseline > 0:
            return float(current / baseline)

    base = v.iloc[max(0, len(v) - 21):-1]
    base = base[np.isfinite(base.to_numpy(dtype=float)) & (base >= 0)]
    baseline = float(base.mean()) if len(base) else 0.0
    return float(current / baseline) if np.isfinite(baseline) and baseline > 0 else 0.0


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
    body_frac: float
    close_pos: float          # 0 = closed on low, 1 = closed on high
    range_atr: float
    htf_bar_ts: Optional[pd.Timestamp] = None
    htf_close: Optional[float] = None
    htf_ema9: Optional[float] = None
    htf_ema21: Optional[float] = None
    htf_pdi: Optional[float] = None
    htf_mdi: Optional[float] = None


def completed_15m_context(df: pd.DataFrame):
    """Return indicators from fully completed 15m bars built only from closed 5m bars."""
    if len(df) < 45:
        return None
    base = df[["open", "high", "low", "close", "volume"]].copy()
    grouped = base.resample("15min", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    })
    counts = base["close"].resample("15min", label="left", closed="left").count()
    # Three source candles and a completed 15m close are both mandatory.
    complete = grouped[(counts == 3) & (grouped.index + pd.Timedelta(minutes=15) <= df.index[-1] + pd.Timedelta(minutes=5))]
    if len(complete) < 30:
        return None
    htf = add_indicators(complete)
    row = htf.iloc[-1]
    values = [row.close, row.ema9, row.ema21, row.pdi, row.mdi]
    if any(pd.isna(value) for value in values):
        return None
    return {
        "htf_bar_ts": htf.index[-1], "htf_close": float(row.close),
        "htf_ema9": float(row.ema9), "htf_ema21": float(row.ema21),
        "htf_pdi": float(row.pdi), "htf_mdi": float(row.mdi),
    }


def compute_features(df: pd.DataFrame, candle_min: int = 5) -> Optional[Features]:
    """Build features only from a clean, ascending sequence of CLOSED OHLCV bars.

    The latest 45 bars feed the rolling indicators and cross/candle metrics. If
    that window contains malformed market data, fail closed instead of allowing
    NaN, infinity, impossible OHLC ranges, or duplicate timestamps into signals.
    """
    required = ("open", "high", "low", "close", "volume")
    if (not isinstance(df, pd.DataFrame) or len(df) < 45
            or not isinstance(df.index, pd.DatetimeIndex)
            or not df.index.is_monotonic_increasing or df.index.has_duplicates
            or any(col not in df.columns for col in required)):
        return None

    window = df.iloc[-45:]
    ohlcv = window.loc[:, required].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(ohlcv).all():
        return None
    op, hi, lo, cl, vol = ohlcv.T
    if ((vol < 0).any() or (hi < lo).any()
            or (hi < np.maximum(op, cl)).any()
            or (lo > np.minimum(op, cl)).any()):
        return None

    r = df.iloc[-1]
    needed = [r.ema9, r.ema21, r.vwap, r.rsi, r.adx, r.pdi, r.mdi, r.atr, r.close]
    if any(pd.isna(x) or not np.isfinite(float(x)) for x in needed):
        return None
    if (r.atr <= 0 or r.close <= 0 or not 0 <= r.rsi <= 100
            or not 0 <= r.adx <= 100 or r.pdi < 0 or r.mdi < 0):
        return None
    slope_ref = df["ema21"].iloc[-4]
    rsi_prev = df["rsi"].iloc[-2]
    adx_prev2 = df["adx"].iloc[-3]
    if any(pd.isna(x) or not np.isfinite(float(x)) for x in (slope_ref, rsi_prev, adx_prev2)):
        return None
    rng = float(r.high - r.low)
    body = abs(float(r.close - r.open))
    n_cross, age = cross_info(df)
    ts = df.index[-1]
    return Features(
        bar_ts=ts,
        bar_end=ts + pd.Timedelta(minutes=candle_min),
        open=float(r.open), high=float(r.high), low=float(r.low), close=float(r.close),
        volume=float(r.volume),
        ema9=float(r.ema9), ema21=float(r.ema21),
        ema21_slope=float(r.ema21 - slope_ref),
        vwap=float(r.vwap),
        rsi=float(r.rsi), rsi_prev=float(rsi_prev),
        adx=float(r.adx), adx_prev2=float(adx_prev2),
        adx_max10=float(df["adx"].iloc[-10:].max()),
        pdi=float(r.pdi), mdi=float(r.mdi),
        atr=float(r.atr), atr_pct=float(r.atr / r.close * 100),
        rvol=rvol_time_of_day(df),
        crosses20=n_cross, cross_age=age,
        body_frac=(body / rng) if rng > 0 else 0.0,
        close_pos=((float(r.close - r.low)) / rng) if rng > 0 else 0.5,
        range_atr=rng / float(r.atr),
    )
