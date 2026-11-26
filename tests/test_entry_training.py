from datetime import date, datetime, timedelta

import pandas as pd

from app.config import IST
from app.db import MemoryStore
from app.entry_training import (
    _evaluate_candidate, _signal_record, build_daily_report, current_report,
    observe_completed_bars, record_signal, set_enabled,
)


class Signal:
    symbol = "ABC"
    side = "LONG"
    entry = 100.0
    sl = 98.0
    tp1 = 103.0
    score = 82
    trigger = "RSI_CROSS"
    indicator_snapshot = {"adx": 25.0, "rvol": 1.4}

    def __init__(self, stamp):
        self.bar_ts = stamp


def _frame(stamp, count):
    index = [stamp + timedelta(minutes=5 * i) for i in range(count + 1)]
    rows = []
    for i in range(count + 1):
        close = 100.0 if i == 0 else 100.25 + (i * 0.05)
        rows.append({
            "open": close - 0.1, "high": close + 0.2,
            "low": close - 0.2, "close": close, "volume": 1000,
        })
    return pd.DataFrame(rows, index=index)


def test_one_day_lab_compares_only_completed_bars_after_signal():
    store = MemoryStore()
    set_enabled(store, True)
    stamp = datetime(2026, 11, 27, 9, 15, tzinfo=IST)
    assert record_signal(store, 7, Signal(stamp), stamp.date())

    # The first scan only has two post-signal candles: no candidate is mature.
    assert observe_completed_bars(store, stamp.date(), {"ABC": _frame(stamp, 2)}) == 0
    first = current_report(store)
    assert first["signals"] == 1
    assert first["pending_signals"] == 1
    assert first["by_delay"] == {}

    # Once eight later candles are complete, all three entry timings can be scored.
    assert observe_completed_bars(store, stamp.date(), {"ABC": _frame(stamp, 8)}) == 3
    report = current_report(store)
    assert report["completed_signals"] == 1
    assert set(report["by_delay"]) == {"0", "1", "2"}
    assert report["by_delay"]["0"]["samples"] == 1
    assert report["recommendation"]["status"] == "insufficient_one_day_samples"


def test_daily_entry_lab_never_uses_signal_candle_as_future_outcome():
    store = MemoryStore()
    set_enabled(store, True)
    stamp = datetime(2026, 11, 27, 9, 15, tzinfo=IST)
    record_signal(store, 1, Signal(stamp), stamp.date())
    frame = _frame(stamp, 0)
    observe_completed_bars(store, stamp.date(), {"ABC": frame})
    report = current_report(store)
    assert report["completed_signals"] == 0
    assert report["by_delay"] == {}


def test_one_day_lab_can_be_disabled_without_affecting_signal_storage():
    store = MemoryStore()
    set_enabled(store, False)
    stamp = datetime(2026, 11, 27, 9, 15, tzinfo=IST)
    assert not record_signal(store, 1, Signal(stamp), stamp.date())
    assert current_report(store)["status"] == "not_started"


def test_daily_report_is_research_only_and_never_proposes_live_changes():
    report = build_daily_report({
        "date": "2026-11-27", "status": "COLLECTING",
        "signals": {}, "config": {},
    })
    assert report["research_only"] is True
    assert report["live_strategy_changed"] is False
    assert report["recommendation"]["status"] == "insufficient_one_day_samples"

def test_pending_symbols_keep_tracking_until_all_shadow_cohorts_mature():
    from app.entry_training import pending_symbols

    store = MemoryStore()
    set_enabled(store, True)
    stamp = datetime(2026, 11, 27, 9, 15, tzinfo=IST)
    record_signal(store, 3, Signal(stamp), stamp.date())
    assert pending_symbols(store, stamp.date()) == {"ABC"}
    observe_completed_bars(store, stamp.date(), {"ABC": _frame(stamp, 8)})
    assert pending_symbols(store, stamp.date()) == set()

def test_training_never_reads_candles_after_completed_scan_boundary():
    store = MemoryStore()
    set_enabled(store, True)
    stamp = datetime(2026, 11, 27, 9, 15, tzinfo=IST)
    record_signal(store, 9, Signal(stamp), stamp.date())
    frame = _frame(stamp, 8)
    boundary = frame.index[3]
    assert observe_completed_bars(
        store, stamp.date(), {"ABC": frame}, through_ts=boundary
    ) == 0
    report = current_report(store)
    assert report["completed_signals"] == 0
    assert report["pending_signals"] == 1

def test_gap_through_stop_uses_adverse_open_fill():
    record = {
        "direction": 1,
        "risk_points": 2.0,
        "original_entry": 100.0,
        "target_r": 1.5,
        "bars": [
            {"ts": f"2026-11-27T09:{20 + i * 5:02d}:00+05:30",
             "open": 97.0 if i == 0 else 97.5,
             "high": 98.0, "low": 96.5, "close": 97.5}
            for i in range(6)
        ],
        "signal_bar_ts": "2026-11-27T09:15:00+05:30",
    }
    result = _evaluate_candidate(record, 0, 0.0)
    assert result["first_hit"] == "SL"
    assert result["gross_r"] == -1.5


def test_invalid_signal_direction_or_target_is_not_trained():
    stamp = datetime(2026, 11, 27, 9, 15, tzinfo=IST)

    class InvalidSide(Signal):
        side = "UNKNOWN"

    class InvalidTarget(Signal):
        tp1 = 99.0

    assert _signal_record(1, InvalidSide(stamp)) is None
    assert _signal_record(2, InvalidTarget(stamp)) is None
