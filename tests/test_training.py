from datetime import date, datetime, timedelta, timezone

from app.training import walk_forward, effective_label, purge_overlapping_outcomes, _score
from app.db import MemoryStore


def _rows(n=1000):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        strong = i % 2 == 0
        day_index, bar_index = divmod(i, 50)
        rows.append({
            "id": i + 1,
            "bar_ts": start + timedelta(days=day_index, minutes=5 * bar_index),
            "status": "SL" if not strong else "TP1_BE",
            "side": "LONG",
            "snapshot": {"adx": 27.0 if strong else 22.5, "rvol": 1.4 if strong else 0.6},
            "net_r": 0.8 if strong else -0.1,
            "auto_label": "WIN" if strong else "LOSS",
            "user_label": None,
        })
    return rows


def test_walk_forward_produces_only_research_filter_diff_with_held_out_check():
    report = walk_forward(_rows())
    assert report["status"] == "proposal_ready"
    assert report["held_out_test"]["n"] >= 12
    assert report["proposal"]["scope"] == "research_filter_only"
    assert "research_filters.min_adx" in report["proposal"]["diff"]


def test_trainer_refuses_small_samples():
    report = walk_forward(_rows(50))
    assert report["status"] == "insufficient_data"
    assert report["proposal"] is None


def test_user_feedback_is_persisted_separately_and_overrides_training_label():
    store = MemoryStore()
    sid = store.add_signal({
        "module": "normal", "symbol": "ABC", "side": "LONG",
        "bar_ts": datetime(2026, 11, 18, 10, tzinfo=timezone.utc),
        "trade_day": date(2026, 11, 18), "entry": 100, "sl": 98,
        "tp1": 103, "tp2": 104, "score": 0, "trig": "rsi",
        "details": {"indicator_snapshot": {"adx": 25, "rvol": 1.3}},
    })
    store.update_outcome(sid, "SL", -1.0)
    assert store.set_feedback(sid, "GOOD")
    row = store.training_rows(date(2026, 11, 1))[0]
    assert row["auto_label"] == "LOSS"
    assert row["user_label"] == "GOOD"
    assert effective_label(row) == "GOOD"
    assert row["snapshot"]["adx"] == 25
    assert row["net_r"] == -1.0
    assert row["first_hit"] == "SL"



def test_open_and_unresolved_signals_are_not_training_samples():
    rows = _rows(1000)
    for row in rows[:100]:
        row["status"] = "OPEN"
        row["net_r"] = None
    report = walk_forward(rows)
    assert report["status"] == "proposal_ready"
    assert report["samples"] == 900


def test_human_feedback_overrides_automatic_categorical_label():
    row = {"auto_label": "LOSS", "user_label": "GOOD"}
    assert effective_label(row) == "GOOD"
    row["user_label"] = "SKIP"
    assert effective_label(row) == "SKIP"


def test_trainer_requires_multiple_distinct_sessions():
    rows = _rows(100)
    report = walk_forward(rows)
    assert report["status"] == "insufficient_distinct_days"
    assert report["proposal"] is None


def test_overlapping_same_symbol_outcome_windows_are_purged():
    start = datetime(2026, 11, 2, 9, 15, tzinfo=timezone.utc)
    rows = [
        {"id": 1, "symbol": "ABC", "bar_ts": start, "bars_held": 3},
        {"id": 2, "symbol": "ABC", "bar_ts": start + timedelta(minutes=10), "bars_held": 2},
        {"id": 3, "symbol": "XYZ", "bar_ts": start + timedelta(minutes=10), "bars_held": 1},
        {"id": 4, "symbol": "ABC", "bar_ts": start + timedelta(minutes=25), "bars_held": 1},
    ]
    kept, purged = purge_overlapping_outcomes(rows)
    assert [row["id"] for row in kept] == [1, 3, 4]
    assert purged == 1


def test_memory_training_rows_include_symbol_and_holding_duration():
    store = MemoryStore()
    sid = store.add_signal({
        "module": "normal", "symbol": "ABC", "side": "LONG",
        "bar_ts": datetime(2026, 11, 18, 10, tzinfo=timezone.utc),
        "trade_day": date(2026, 11, 18), "entry": 100, "sl": 98,
        "tp1": 103, "tp2": 104, "score": 0, "trig": "rsi",
        "details": {"indicator_snapshot": {"adx": 25, "rvol": 1.3}},
    })
    store.update_excursions(sid, 1.5, 0.4, 3)
    row = store.training_rows(date(2026, 11, 1))[0]
    assert row["symbol"] == "ABC"
    assert row["bars_held"] == 3


def test_score_reports_profit_factor_and_drawdown_from_net_r():
    rows = [{"net_r": 1.0}, {"net_r": -2.0}, {"net_r": 1.0}]
    report = _score(rows)
    assert report["sum_net_r"] == 0.0
    assert report["profit_factor"] == 1.0
    assert report["max_drawdown_r"] == 2.0
    assert report["avg_win_r"] == 1.0
    assert report["avg_loss_r"] == -2.0
