from datetime import date, datetime, time, timezone

import pandas as pd

from app.db import MemoryStore
from app.tracker import measure_excursions, measure_target_times


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


def test_measure_target_times_ignores_targets_after_session_end():
    df = _frame([
        [100, 102, 99, 101, 1000],       # 09:20-09:25, final eligible candle
        [101, 105, 100.5, 104, 1000],   # 09:25-09:30, after session cutoff
    ])
    assert measure_target_times(_signal(), df, session_end=time(9, 25)) == (None, None)


def test_measure_target_times_counts_tp1_on_final_candle_but_not_later_tp2():
    df = _frame([
        [100, 103.5, 100.5, 103, 1000], # TP1 reached before 09:25 close
        [103, 105, 102, 104.5, 1000],   # TP2 occurs after session cutoff
    ])
    assert measure_target_times(_signal(), df, session_end=time(9, 25)) == (1, None)



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



def test_memory_store_persists_option_tracking_and_manual_actual_costs():
    store = MemoryStore()
    sid = store.add_signal({
        "module": "normal", "symbol": "ABC", "side": "LONG",
        "bar_ts": datetime(2026, 11, 18, 10, tzinfo=timezone.utc),
        "trade_day": date(2026, 11, 18), "entry": 100, "sl": 98,
        "tp1": 103, "tp2": 104, "score": 0, "trig": "rsi",
        "option_tracking": {"instrument_key": "NSE_FO|ABC", "entry_price": 12.5, "mfe_pct": 8.0},
        "details": {"indicator_snapshot": {"adx": 25}},
    })
    store.update_option_tracking(sid, {
        "instrument_key": "NSE_FO|ABC", "entry_price": 12.5, "mfe_pct": 12.0,
        "mae_pct": -4.0, "tracking_status": "TRACKING",
    })
    assert store.update_actual_option_costs(sid, 50, 80.0, 40.0, 30.0, "2026-11-19T10:00:00+05:30")
    row = store.training_rows(date(2026, 11, 1))[0]
    assert row["option_tracking"]["mfe_pct"] == 12.0
    assert row["actual_option_costs"]["brokerage_rupees"] == 80.0
    assert row["actual_option_costs"]["other_fees_rupees"] == 40.0
    assert row["actual_option_costs"]["total_cost_rupees"] == 150.0
    assert row["actual_option_costs"]["cost_per_unit"] == 3.0


def test_memory_store_rejects_invalid_actual_cost_inputs():
    store = MemoryStore()
    assert not store.update_actual_option_costs(1, 0, 5.0, 5.0, 2.0, "2026-11-19T10:00:00+05:30")
    assert not store.update_actual_option_costs(1, 10, -1.0, 0.0, 2.0, "2026-11-19T10:00:00+05:30")


def test_memory_store_requires_option_contract_for_actual_costs():
    store = MemoryStore()
    sid = store.add_signal({
        "module": "normal", "symbol": "ABC", "side": "LONG",
        "bar_ts": datetime(2026, 11, 18, 10, tzinfo=timezone.utc),
        "trade_day": date(2026, 11, 18), "entry": 100, "sl": 98,
        "tp1": 103, "tp2": 104, "score": 0, "trig": "rsi",
        "details": {},
    })
    assert not store.update_actual_option_costs(sid, 50, 80.0, 40.0, 30.0, "2026-11-19T10:00:00+05:30")


def test_memory_training_rows_include_score_trigger_and_excursions():
    store = MemoryStore()
    sid = store.add_signal({
        "module": "normal", "symbol": "ABC", "side": "LONG",
        "bar_ts": datetime(2026, 11, 18, 10, tzinfo=timezone.utc),
        "trade_day": date(2026, 11, 18), "entry": 100, "sl": 98,
        "tp1": 103, "tp2": 104, "score": 87, "trig": "MOMENTUM_EXPLOSION",
        "details": {"indicator_snapshot": {"adx": 25}},
    })
    store.update_excursions(sid, 2.1, 0.4, 5)
    row = store.training_rows(date(2026, 11, 1))[0]
    assert row["score"] == 87
    assert row["trig"] == "MOMENTUM_EXPLOSION"
    assert row["mfe_r"] == 2.1
    assert row["mae_r"] == 0.4
    assert row["bars_held"] == 5


def test_excursions_exclude_unknown_favourable_extreme_on_stop_bar():
    df = _frame([[100, 110, 97, 99, 1000]])
    mfe, mae, bars, last = measure_excursions(_signal(), df)
    assert mfe == 0.0
    assert mae == 1.0
    assert bars == 1
    assert last == df.index[-1].to_pydatetime()


def test_short_excursions_exclude_unknown_favourable_extreme_on_stop_bar():
    df = _frame([[100, 103, 90, 101, 1000]])
    mfe, mae, bars, _ = measure_excursions(_signal("SHORT"), df)
    assert mfe == 0.0
    assert mae == 1.0
    assert bars == 1


def test_excursions_use_known_tp1_and_tp2_barriers_not_full_exit_bar_range():
    tp1_be = _frame([[100, 105, 99.5, 100, 1000]])
    assert measure_excursions(_signal(), tp1_be)[:3] == (1.5, 0.0, 1)

    tp2 = _frame([[100, 105, 100.5, 104.5, 1000]])
    assert measure_excursions(_signal(), tp2)[:3] == (2.0, 0.0, 1)


def test_excursions_keep_full_ranges_before_terminal_bar_only():
    df = _frame([[100, 102, 99, 101, 1000], [100, 110, 97, 99, 1000]])
    mfe, mae, bars, _ = measure_excursions(_signal(), df)
    assert mfe == 1.0
    assert mae == 1.0
    assert bars == 2



def test_eod_terminal_bar_uses_close_not_unordered_high_low():
    df = _frame([[100, 101, 99, 100.5, 1000]])
    mfe, mae, bars, _ = measure_excursions(
        _signal(), df, session_end=time(9, 25)
    )
    assert mfe == 0.25
    assert mae == 0.0
    assert bars == 1
