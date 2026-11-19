from datetime import date, datetime
from datetime import timezone, timedelta

from app.db import MemoryStore


IST = timezone(timedelta(hours=5, minutes=30))


def _add(store, symbol, side, status, rr, hhmm, trigger="test"):
    sid = store.add_signal({
        "module": "normal",
        "symbol": symbol,
        "side": side,
        "bar_ts": datetime.fromisoformat(f"2026-11-18T{hhmm}:00+05:30"),
        "trade_day": date(2026, 11, 18),
        "entry": 100.0,
        "sl": 98.0,
        "tp1": 103.0,
        "tp2": 105.0,
        "score": 0,
        "trig": trigger,
    })
    if status != "OPEN":
        store.update_outcome(sid, status, rr)
    return sid


def test_detailed_performance_report_includes_outcomes_breakdowns_and_drawdown():
    store = MemoryStore()
    first = _add(store, "AAA", "LONG", "TP2", 2.0, "10:00", "breakout")
    second = _add(store, "BBB", "SHORT", "SL", -1.0, "10:30", "retest")
    third = _add(store, "AAA", "LONG", "OPEN", None, "11:00", "continuation")

    store.update_excursions(first, 2.2, 0.3, 4)
    store.update_excursions(second, 0.2, 1.1, 2)
    store.update_excursions(third, 0.3, 0.5, 6, True)

    report = store.performance_report(date(2026, 11, 1))
    summary = report["summary"]

    assert summary["total_signals"] == 3
    assert summary["call_signals"] == 2
    assert summary["put_signals"] == 1
    assert summary["closed"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["win_rate_pct"] == 50.0
    assert summary["avg_r"] == 0.5
    assert summary["tp1_hits"] == 1
    assert summary["tp2_hits"] == 1
    assert summary["stop_hits"] == 1
    assert summary["time_stop_review_due"] == 1
    assert summary["max_drawdown_r"] == 1.0
    assert report["by_symbol"]["AAA"]["signals"] == 2
    assert report["by_setup"]["breakout"]["closed"] == 1
    assert report["by_time_of_day"]["10:00-10:59"]["signals"] == 2
