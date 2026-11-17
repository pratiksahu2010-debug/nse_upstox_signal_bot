"""Outcome tracking: replays each open alert against the bars that followed it.

Rules (deliberately conservative):
  * entry = signal-bar close; stop = SL; if SL and TP1 are touched in the same bar, SL wins.
  * TP1 books 50 %; the stop of the remainder moves to entry. If entry and TP2 are touched
    in the same later bar, entry (breakeven) wins.
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
                stage = "TP1"
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
