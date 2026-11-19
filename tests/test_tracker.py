from datetime import date, datetime, timezone

import pandas as pd

from app.db import MemoryStore
from app.tracker import measure_target_times


def _frame(rows):
    index = pd.date_range("2026-11-18 09:20", periods=len(rows), freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame(rows, index=index, columns=["open", "high", "low", "close", "volume"])


def _signal(side="LONG"):
    if side == "LONG":
        return {"side": "LONG", "bar_ts": pd.Timestamp("2026-11-18 09:15", tz="Asia/Kolkata"),
                "entry": 100.0, "sl": 98.0, "tp1": 103.0, "tp2": 104.0}
    return {"side": "SHORT", "bar_ts": pd.Timestamp("2026-11-18 09:15", tz="Asia/Kolkata"),
            "entry": 100.0, "sl": 102.0, "tp1": 97.0, "tp2": 96.0}


def test_measure_target_times_tracks_long_targets_in_bars():
    df = _frame([[100, 102, 99, 101, 1000], [101, 104.5, 100.5, 104, 1000]])
    assert measure_target_times(_signal(), df) == (2, 2)


def test_measure_target_times_sl_wins_same_bar_tp1_collision():
    df = _frame([[100, 104, 97, 101, 1000]])
    assert measure_target_times(_signal(), df) == (None, None)


def test_measure_target_times_tp1_then_breakeven_does_not_count_tp2():
    df = _frame([[100, 103.5, 100.1, 103, 1000], [103, 103.2, 99.8, 100, 1000],
                 [100, 105, 99.9, 104, 1000]])
    assert measure_target_times(_signal(), df) == (1, None)


def test_measure_target_times_tracks_short_targets():
    df = _frame([[100, 99.9, 95.5, 96, 1000]])
    assert measure_target_times(_signal("SHORT"), df) == (1, 1)



def test_memory_store_persists_target_time_metrics_in_training_rows():
    store = MemoryStore()
    sid = store.add_signal({
        "module": "normal", "symbol": "ABC", "side": "LONG",
        "bar_ts": datetime(2026, 11, 18, 10, tzinfo=timezone.utc),
        "trade_day": date(2026, 11, 18), "entry": 100, "sl": 98,
        "tp1": 103, "tp2": 104, "score": 0, "trig": "rsi",
        "details": {"indicator_snapshot": {"adx": 25}},
    })
    store.update_target_times(sid, 3, 7)
    row = store.training_rows(date(2026, 11, 1))[0]
    assert row["bars_to_tp1"] == 3
    assert row["bars_to_tp2"] == 7
