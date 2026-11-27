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


def measure_target_times(
    row: dict,
    df: pd.DataFrame,
    session_end: Optional[time] = None,
    candle_min: int = 5,
):
    """Return bars to TP1/TP2, stopping at the same session cutoff as replay.

    A target touched in the final eligible candle counts. Candles beginning
    after session_end never count, keeping research metrics consistent with
    simulate()'s end-of-session close.
    """
    risk = abs(float(row["entry"]) - float(row["sl"]))
    if risk <= 0:
        return None, None
    side = 1 if row["side"] == "LONG" else -1
    entry = float(row["entry"])
    sl, tp1, tp2 = float(row["sl"]), float(row["tp1"]), float(row["tp2"])
    after = df[df.index > _ts(row["bar_ts"])]
    stage, bars_held = "OPEN", 0
    bars_to_tp1 = bars_to_tp2 = None
    for bar_ts, bar in after.iterrows():
        bars_held += 1
        low, high = float(bar["low"]), float(bar["high"])
        bar_end = (bar_ts + pd.Timedelta(minutes=candle_min)).time()
        eod = session_end is not None and bar_end >= session_end
        if stage == "OPEN":
            hit_sl = low <= sl if side > 0 else high >= sl
            hit_tp1 = high >= tp1 if side > 0 else low <= tp1
            if hit_sl:
                break
            if hit_tp1:
                bars_to_tp1 = bars_held
                stage = "TP1"
                hit_be = low <= entry if side > 0 else high >= entry
                hit_tp2 = high >= tp2 if side > 0 else low <= tp2
                if hit_be:
                    break
                if hit_tp2:
                    bars_to_tp2 = bars_held
                    break
        else:
            hit_be = low <= entry if side > 0 else high >= entry
            hit_tp2 = high >= tp2 if side > 0 else low <= tp2
            if hit_be:
                break
            if hit_tp2:
                bars_to_tp2 = bars_held
                break
        if eod:
            break
    return bars_to_tp1, bars_to_tp2


def measure_excursions(
    row: dict,
    df: pd.DataFrame,
    session_end: Optional[time] = None,
    candle_min: int = 5,
):
    """Return (MFE_R, MAE_R, completed_bars_held, last_bar_ts) after entry.

    Completed candles before exit contribute their full range. The terminal candle
    contributes only the known exit barrier (or its close for EOD): OHLC cannot tell
    whether the opposite extreme occurred before or after the exit.
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
    r1, r2 = abs(tp1 - entry) / risk, abs(tp2 - entry) / risk
    stage = "OPEN"
    exit_ts = None
    terminal_mfe = terminal_mae = 0.0

    for ts, bar in after.iterrows():
        low, high, close = float(bar["low"]), float(bar["high"]), float(bar["close"])
        bar_end = (ts + pd.Timedelta(minutes=candle_min)).time()
        eod = session_end is not None and bar_end >= session_end
        if stage == "OPEN":
            hit_sl = low <= sl if side > 0 else high >= sl
            hit_tp1 = high >= tp1 if side > 0 else low <= tp1
            if hit_sl:
                # The stop price is known; the other extreme's timing is not.
                terminal_mae = 1.0
                exit_ts = ts
                break
            if hit_tp1:
                stage = "TP1"
                hit_be = low <= entry if side > 0 else high >= entry
                hit_tp2 = high >= tp2 if side > 0 else low <= tp2
                if hit_be:
                    terminal_mfe = r1
                    exit_ts = ts
                    break
                if hit_tp2:
                    terminal_mfe = r2
                    exit_ts = ts
                    break
                if eod:
                    terminal_mfe = max(r1, side * (close - entry) / risk, 0.0)
                    terminal_mae = max(-side * (close - entry) / risk, 0.0)
                    exit_ts = ts
                    break
            elif eod:
                terminal_mfe = max(side * (close - entry) / risk, 0.0)
                terminal_mae = max(-side * (close - entry) / risk, 0.0)
                exit_ts = ts
                break
        else:
            hit_be = low <= entry if side > 0 else high >= entry
            hit_tp2 = high >= tp2 if side > 0 else low <= tp2
            if hit_be:
                terminal_mfe = r1
                exit_ts = ts
                break
            if hit_tp2:
                terminal_mfe = r2
                exit_ts = ts
                break
            if eod:
                terminal_mfe = max(r1, side * (close - entry) / risk, 0.0)
                terminal_mae = max(-side * (close - entry) / risk, 0.0)
                exit_ts = ts
                break

    terminal_count = 0
    if exit_ts is not None:
        terminal_count = 1
        completed = after.loc[after.index < exit_ts]
    else:
        completed = after

    if completed.empty:
        mfe_r = mae_r = 0.0
    elif side > 0:
        mfe_r = max(0.0, float(((completed["high"].astype(float) - entry) / risk).max()))
        mae_r = max(0.0, float(((entry - completed["low"].astype(float)) / risk).max()))
    else:
        mfe_r = max(0.0, float(((entry - completed["low"].astype(float)) / risk).max()))
        mae_r = max(0.0, float(((completed["high"].astype(float) - entry) / risk).max()))

    mfe_r = max(mfe_r, terminal_mfe)
    mae_r = max(mae_r, terminal_mae)
    counted = len(completed) + terminal_count
    last_bar = (exit_ts if exit_ts is not None else completed.index[-1]).to_pydatetime() if counted else None
    return mfe_r, mae_r, int(counted), last_bar
