"""Synthetic 5-minute market data for tests (no network)."""
import numpy as np
import pandas as pd

from datetime import timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def session_index(day: pd.Timestamp, start="09:15", end="15:30") -> pd.DatetimeIndex:
    s = pd.Timestamp(f"{day:%Y-%m-%d} {start}", tz=IST)
    e = pd.Timestamp(f"{day:%Y-%m-%d} {end}", tz=IST)
    return pd.date_range(s, e, freq="5min", inclusive="left")


def tod_volume_shape(idx, base):
    mins = np.array([(t.hour * 60 + t.minute) for t in idx])
    open_m = mins.min()
    x = (mins - open_m) / max(1, mins.max() - open_m)
    shape = 1.0 + 1.6 * np.exp(-x * 14) + 0.8 * np.exp(-(1 - x) * 10)    # U-shape
    return base * shape


def make_day(day, start_price, rng, mode="chop", base_vol=100_000, vol_pct=0.0015, mom_k=0.3,
             start="09:15", end="15:30", drift_pct=0.0003):
    idx = session_index(day, start, end)
    n = len(idx)
    ret = np.zeros(n)
    mom = 0.0
    for i in range(n):
        shock = rng.normal(0, vol_pct)
        if mode == "trend_up":
            mom = mom_k * mom + drift_pct * (1 if (i // 9) % 5 != 3 else -0.9)   # periodic pullback
        elif mode == "trend_down":
            mom = mom_k * mom - drift_pct * (1 if (i // 9) % 5 != 3 else -0.9)
        else:
            mom = -0.3 * mom
        ret[i] = shock + mom
    close = start_price * np.exp(np.cumsum(ret))
    open_ = np.r_[start_price, close[:-1]]
    spread = np.abs(rng.normal(0, vol_pct * 0.6, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    vol = tod_volume_shape(idx, base_vol) * rng.lognormal(0, 0.25, n)
    if mode.startswith("trend"):
        vol = vol * (1 + 1.2 * np.abs(ret) / vol_pct * 0.5)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol.round()}, index=idx)


def make_history(n_days=7, seed=1, last_mode="chop", start_price=1000.0, **kw):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(end=pd.Timestamp("2026-11-16"), periods=n_days)
    frames, px = [], start_price
    for i, d in enumerate(days):
        mode = last_mode if i == len(days) - 1 else "chop"
        f = make_day(d, px, rng, mode=mode, **kw)
        px = float(f["close"].iloc[-1]) * (1 + rng.normal(0, 0.002))
        frames.append(f)
    return pd.concat(frames)
