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
        "status": "TP2",
        "snapshot": {"adx": 25.0},
        "option_tracking": {"instrument_key": "NSE_FO|RELIANCE"},
        "actual_option_costs": {"total_cost_rupees": 12.5},
    }])
    row = next(csv.DictReader(io.StringIO(content)))

    assert row["module"] == "normal"
    assert row["symbol"] == "RELIANCE"
    assert row["side"] == "LONG"
    assert row["snapshot"] == '{"adx":25.0}'
    assert row["option_tracking"] == '{"instrument_key":"NSE_FO|RELIANCE"}'
    assert row["actual_option_costs"] == '{"total_cost_rupees":12.5}'
