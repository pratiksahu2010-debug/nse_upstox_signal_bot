from app.telegram_polling import get_persistent_state, set_persistent_state
from app.db import MemoryStore


def test_polling_cursor_and_pause_state_persist_in_memory_store():
    store = MemoryStore()
    assert get_persistent_state(store, "telegram_update_offset", 0) == 0
    set_persistent_state(store, "telegram_update_offset", 123)
    set_persistent_state(store, "paused", True)
    assert get_persistent_state(store, "telegram_update_offset", 0) == 123
    assert get_persistent_state(store, "paused", False) is True


def test_training_export_includes_module_and_underlying_symbol():
    import csv
    import io

    from app.telegram_polling import build_training_export_csv

    content = build_training_export_csv([{
        "id": 42,
        "module": "normal",
        "symbol": "RELIANCE",
        "bar_ts": "2026-11-19T10:15:00+05:30",
        "side": "LONG",
        "score": 87,
        "trig": "MOMENTUM_EXPLOSION",
        "status": "TP2",
        "mfe_r": 2.1,
        "mae_r": 0.4,
        "bars_held": 5,
        "snapshot": {"adx": 25.0},
        "option_tracking": {"instrument_key": "NSE_FO|RELIANCE"},
        "actual_option_costs": {"total_cost_rupees": 12.5},
    }])
    row = next(csv.DictReader(io.StringIO(content)))

    assert row["module"] == "normal"
    assert row["symbol"] == "RELIANCE"
    assert row["side"] == "LONG"
    assert row["score"] == "87"
    assert row["trig"] == "MOMENTUM_EXPLOSION"
    assert row["mfe_r"] == "2.1"
    assert row["mae_r"] == "0.4"
    assert row["bars_held"] == "5"
    assert row["snapshot"] == '{"adx":25.0}'
    assert row["option_tracking"] == '{"instrument_key":"NSE_FO|RELIANCE"}'
    assert row["actual_option_costs"] == '{"total_cost_rupees":12.5}'


def test_postgres_training_rows_keep_module_and_symbol_columns_aligned():
    from datetime import date, datetime, timezone

    from app.db import PostgresStore

    store = PostgresStore.__new__(PostgresStore)
    store._run = lambda sql, params, fetch: [(
        42, "normal", datetime(2026, 11, 19, 10, 15, tzinfo=timezone.utc),
        "RELIANCE", "LONG", 87, "MOMENTUM_EXPLOSION", 3, 2.1, 0.4,
        {"adx": 25.0}, "TP2", 2.0, 1.9, "WIN", "GOOD", "TP1", 1, 3,
        {"instrument_key": "NSE_FO|RELIANCE"},
        {"total_cost_rupees": 12.5},
    )]

    row = store.training_rows(date(2026, 11, 1))[0]

    assert row["module"] == "normal"
    assert row["symbol"] == "RELIANCE"
    assert row["side"] == "LONG"
    assert row["score"] == 87
    assert row["trig"] == "MOMENTUM_EXPLOSION"
    assert row["mfe_r"] == 2.1
    assert row["mae_r"] == 0.4
    assert row["bars_held"] == 3
    assert row["snapshot"] == {"adx": 25.0}
    assert row["option_tracking"]["instrument_key"] == "NSE_FO|RELIANCE"
    assert row["actual_option_costs"]["total_cost_rupees"] == 12.5
