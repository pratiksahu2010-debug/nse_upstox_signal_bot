"""Outcome tracking: replays each open alert against the bars that followed it.

Rules (deliberately conservative):
  * entry = signal-bar close; stop = SL; if SL and TP1 are touched in the same bar, SL wins.
  * TP1 books 50 %; the stop of the remainder moves to entry. If TP1 and entry are both
    touched in one OHLC bar, breakeven wins for the remainder; if entry and TP2 are both
    touched in a later bar, breakeven also wins.
  * Anything still open at session_end is closed at that bar's close ('EOD').
R result: SL = -1R; TP1_BE = +0.5*R1; TP2 = 0.5*R1 + 0.5*R2; EOD = mark-to-market.
The replay always starts from the signal bar, so it is idempotent across cycles/restarts.
"""
from datetime import time
from typing import Optional

import pandas as pd

from .config import IST


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)


def simulate(row: dict, df: pd.DataFrame, session_end: time, candle_min: int = 5):
    """Return (status, r_result). status: OPEN | TP1 | SL | TP1_BE | TP2 | EOD."""
    s = 1 if row["side"] == "LONG" else -1
    entry, sl, tp1, tp2 = row["entry"], row["sl"], row["tp1"], row["tp2"]
    risk = abs(entry - sl)
    if risk <= 0:
        return "OPEN", None
    r1 = abs(tp1 - entry) / risk
    r2 = abs(tp2 - entry) / risk
    after = df[df.index > _ts(row["bar_ts"])]
    stage = "OPEN"
    for ts, b in after.iterrows():
        lo, hi, cl = float(b["low"]), float(b["high"]), float(b["close"])
        end = (ts + pd.Timedelta(minutes=candle_min)).time()
        if stage == "OPEN":
            hit_sl = lo <= sl if s > 0 else hi >= sl
            hit_tp1 = hi >= tp1 if s > 0 else lo <= tp1
            if hit_sl:
                return "SL", -1.0
            if hit_tp1:
                # TP1 books half; the remaining half is protected at breakeven.
                # OHLC cannot reveal the intrabar path, so resolve same-bar
                # breakeven/TP2 collisions conservatively (breakeven wins).
                stage = "TP1"
                hit_be = lo <= entry if s > 0 else hi >= entry
                hit_tp2 = hi >= tp2 if s > 0 else lo <= tp2
                if hit_be:
                    return "TP1_BE", 0.5 * r1
                if hit_tp2:
                    return "TP2", 0.5 * r1 + 0.5 * r2
                if end >= session_end:
                    return "EOD", 0.5 * r1 + 0.5 * s * (cl - entry) / risk
                continue
            if end >= session_end:
                return "EOD", s * (cl - entry) / risk
        else:
            hit_be = lo <= entry if s > 0 else hi >= entry
            hit_tp2 = hi >= tp2 if s > 0 else lo <= tp2
            if hit_be:
                return "TP1_BE", 0.5 * r1
            if hit_tp2:
                return "TP2", 0.5 * r1 + 0.5 * r2
            if end >= session_end:
                return "EOD", 0.5 * r1 + 0.5 * s * (cl - entry) / risk
    if stage == "TP1":
        return "TP1", 0.5 * r1
    return "OPEN", None


def measure_excursions(
    row: dict,
    df: pd.DataFrame,
    session_end: Optional[time] = None,
    candle_min: int = 5,
):
    """Return (MFE_R, MAE_R, completed_bars_held, last_bar_ts) after entry.

    The signal candle is excluded. Replay stops at the first bar that closes the trade
    under the same conservative SL/TP1/BE/TP2/EOD ordering used by simulate(), so
    post-exit bars are never included in excursion metrics.
    """
    risk = abs(float(row["entry"]) - float(row["sl"]))
    if risk <= 0:
        return 0.0, 0.0, 0, None

    after = df[df.index > _ts(row["bar_ts"])]
    if after.empty:
        return 0.0, 0.0, 0, None

    side = 1 if row["side"] == "LONG" else -1
    entry = float(row["entry"])
    sl, tp1, tp2 = float(row["sl"]), float(row["tp1"]), float(row["tp2"])
    stage = "OPEN"
    exit_ts = None
    for ts, bar in after.iterrows():
        low, high, close = float(bar["low"]), float(bar["high"]), float(bar["close"])
        bar_end = (ts + pd.Timedelta(minutes=candle_min)).time()
        if stage == "OPEN":
            hit_sl = low <= sl if side > 0 else high >= sl
            hit_tp1 = high >= tp1 if side > 0 else low <= tp1
            if hit_sl:
                exit_ts = ts
                break
            if hit_tp1:
                stage = "TP1"
                hit_be = low <= entry if side > 0 else high >= entry
                hit_tp2 = high >= tp2 if side > 0 else low <= tp2
                if hit_be or hit_tp2 or (session_end is not None and bar_end >= session_end):
                    exit_ts = ts
                    break
            elif session_end is not None and bar_end >= session_end:
                exit_ts = ts
                break
        else:
            hit_be = low <= entry if side > 0 else high >= entry
            hit_tp2 = high >= tp2 if side > 0 else low <= tp2
            if hit_be or hit_tp2 or (session_end is not None and bar_end >= session_end):
                exit_ts = ts
                break

    if exit_ts is not None:
        after = after.loc[:exit_ts]
    if after.empty:
        return 0.0, 0.0, 0, None

    if side > 0:
        favourable = (after["high"].astype(float) - entry) / risk
        adverse = (entry - after["low"].astype(float)) / risk
    else:
        favourable = (entry - after["low"].astype(float)) / risk
        adverse = (after["high"].astype(float) - entry) / risk

    mfe_r = max(0.0, float(favourable.max()))
    mae_r = max(0.0, float(adverse.max()))
    last_bar = after.index[-1].to_pydatetime()
    return mfe_r, mae_r, int(len(after)), last_bar
