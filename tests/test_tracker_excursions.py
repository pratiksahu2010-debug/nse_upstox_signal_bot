from datetime import date

import pandas as pd

from app.config import IST
from app.db import MemoryStore
from app.tracker import measure_excursions


def _bars():
    index = pd.DatetimeIndex([
        pd.Timestamp("2026-11-17 10:20", tz=IST),
        pd.Timestamp("2026-11-17 10:25", tz=IST),
        pd.Timestamp("2026-11-17 10:30", tz=IST),
    ])
    return pd.DataFrame(
        {
            "high": [150.0, 103.0, 102.0],
            "low": [50.0, 101.0, 97.0],
            "close": [100.0, 102.0, 101.0],
        },
        index=index,
    )


def test_mfe_mae_excludes_signal_candle_for_long_trade():
    row = {
        "side": "LONG",
        "bar_ts": pd.Timestamp("2026-11-17 10:20", tz=IST),
        "entry": 100.0,
        "sl": 98.0,
        "tp1": 103.0,
        "tp2": 104.0,
    }
    mfe, mae, bars, last_bar = measure_excursions(row, _bars())

    assert mfe == 1.5
    assert mae == 1.5
    assert bars == 2
    assert last_bar == pd.Timestamp("2026-11-17 10:30", tz=IST).to_pydatetime()


def test_mfe_mae_uses_inverse_direction_for_short_trade():
    row = {
        "side": "SHORT",
        "bar_ts": pd.Timestamp("2026-11-17 10:20", tz=IST),
        "entry": 100.0,
        "sl": 102.0,
        "tp1": 97.0,
        "tp2": 96.0,
    }
    index = pd.DatetimeIndex([
        pd.Timestamp("2026-11-17 10:20", tz=IST),
        pd.Timestamp("2026-11-17 10:25", tz=IST),
        pd.Timestamp("2026-11-17 10:30", tz=IST),
    ])
    short_bars = pd.DataFrame(
        {"high": [150.0, 101.5, 100.5], "low": [50.0, 99.5, 97.0], "close": [100.0, 100.0, 98.0]},
        index=index,
    )
    mfe, mae, bars, _ = measure_excursions(row, short_bars)

    assert mfe == 1.5
    assert mae == 0.75
    assert bars == 2


def test_memory_store_keeps_best_excursion_seen():
    store = MemoryStore()
    sid = store.add_signal({
        "module": "normal",
        "symbol": "TEST",
        "side": "LONG",
        "bar_ts": pd.Timestamp("2026-11-17 10:20", tz=IST).to_pydatetime(),
        "trade_day": date(2026, 11, 17),
        "entry": 100.0,
        "sl": 98.0,
        "tp1": 103.0,
        "tp2": 104.0,
        "score": 0,
        "trig": "test",
    })

    store.update_excursions(sid, 1.5, 0.6, 2)
    store.update_excursions(sid, 1.0, 0.2, 1)
    row = store.rows[0]

    assert row["mfe_r"] == 1.5
    assert row["mae_r"] == 0.6
    assert row["bars_held"] == 2


def test_mfe_mae_stops_at_exit_bar_and_ignores_later_price_extremes():
    index = pd.DatetimeIndex([
        pd.Timestamp("2026-11-17 10:20", tz=IST),
        pd.Timestamp("2026-11-17 10:25", tz=IST),
        pd.Timestamp("2026-11-17 10:30", tz=IST),
    ])
    frame = pd.DataFrame(
        {"high": [150.0, 101.0, 200.0], "low": [50.0, 97.0, 99.0], "close": [100.0, 98.0, 150.0]},
        index=index,
    )
    row = {
        "side": "LONG",
        "bar_ts": pd.Timestamp("2026-11-17 10:20", tz=IST),
        "entry": 100.0,
        "sl": 98.0,
        "tp1": 105.0,
        "tp2": 110.0,
    }

    mfe, mae, bars, last_bar = measure_excursions(row, frame)

    assert mfe == 0.5
    assert mae == 1.5
    assert bars == 1
    assert last_bar == pd.Timestamp("2026-11-17 10:25", tz=IST).to_pydatetime()


def test_stats_exposes_average_mfe_and_mae_for_module():
    store = MemoryStore()
    sid = store.add_signal({
        "module": "normal",
        "symbol": "TEST",
        "side": "LONG",
        "bar_ts": pd.Timestamp("2026-11-17 10:20", tz=IST).to_pydatetime(),
        "trade_day": date(2026, 11, 17),
        "entry": 100.0,
        "sl": 98.0,
        "tp1": 103.0,
        "tp2": 104.0,
        "score": 0,
        "trig": "test",
    })
    store.update_excursions(sid, 1.5, 0.5, 3)
    store.update_outcome(sid, "TP2", 2.0)

    row = store.stats(date(2026, 11, 1))[0]

    assert row["avg_mfe_r"] == 1.5
    assert row["avg_mae_r"] == 0.5
    assert row["closed"] == 1


def test_memory_store_persists_time_stop_review_flag():
    store = MemoryStore()
    sid = store.add_signal({
        "module": "normal", "symbol": "TEST", "side": "LONG",
        "bar_ts": pd.Timestamp("2026-11-17 10:20", tz=IST).to_pydatetime(),
        "trade_day": date(2026, 11, 17), "entry": 100.0, "sl": 98.0,
        "tp1": 103.0, "tp2": 105.0, "score": 0, "trig": "test",
    })
    store.update_excursions(sid, 0.2, 0.4, 6, True)
    assert store.rows[0]["time_stop_review_due"] is True
