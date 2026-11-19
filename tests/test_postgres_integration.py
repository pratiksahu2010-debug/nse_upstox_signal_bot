"""Real PostgreSQL persistence and migration integration coverage.

The CI job supplies an isolated disposable database through TEST_DATABASE_URL.
The test is skipped in local/unit-only runs without that explicit variable.
"""
import os
from datetime import date, datetime, timezone

import pytest

from app.db import PostgresStore


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires isolated TEST_DATABASE_URL")
def test_postgres_migrations_and_signal_lifecycle_round_trip():
    store = PostgresStore(os.environ["TEST_DATABASE_URL"])
    signal_id = None
    initialized = False
    today = date.today()
    stamp = datetime.now(timezone.utc)
    symbol = "CI_POSTGRES_INTEGRATION"

    try:
        store.init()
        initialized = True
        # Re-running init must be safe and must not reapply completed migrations.
        store.init()
        versions = store._run("SELECT version FROM schema_migrations ORDER BY version", fetch="all")
        assert [row[0] for row in versions] == list(range(1, 8))

        store.save_normal_state(today, symbol, {"phase": "TRIGGERED", "side": 1})
        assert store.load_normal_states(today)[symbol] == {"phase": "TRIGGERED", "side": 1}

        signal = {
            "module": "normal",
            "symbol": symbol,
            "side": "LONG",
            "bar_ts": stamp,
            "trade_day": today,
            "entry": 100.0,
            "sl": 98.0,
            "tp1": 102.0,
            "tp2": 104.0,
            "score": 88,
            "trig": "CI_INTEGRATION",
            "details": {"indicator_snapshot": {"adx": 25.0, "rsi": 61.0}},
            "cost_r": 0.1,
            "option_tracking": {"instrument_key": "NSE_FO|CI_TEST"},
        }
        signal_id = store.add_signal(signal)
        assert signal_id is not None
        assert store.add_signal(signal) is None  # same-side duplicate is rejected
        opposite_side = dict(signal, side="SHORT")
        assert store.add_signal(opposite_side) is None  # one direction per symbol/candle

        assert store.set_feedback(signal_id, "GOOD") is True
        store.update_excursions(signal_id, 1.2, 0.4, 3, True)
        store.update_target_times(signal_id, 1, 3)
        store.update_outcome(signal_id, "TP2", 1.5)
        assert store.update_actual_option_costs(
            signal_id, 25, 20.0, 2.0, 3.0, stamp.isoformat()
        ) is True

        rows = store.training_rows(today)
        row = next(item for item in rows if item["id"] == signal_id)
        assert row["module"] == "normal"
        assert row["symbol"] == symbol
        assert row["side"] == "LONG"
        assert row["snapshot"] == {"adx": 25.0, "rsi": 61.0}
        assert row["status"] == "TP2"
        assert row["gross_r"] == 1.5
        assert row["net_r"] == pytest.approx(1.4)
        assert row["auto_label"] == "WIN"
        assert row["user_label"] == "GOOD"
        assert row["first_hit"] == "TP1"
        assert row["bars_to_tp1"] == 1
        assert row["bars_to_tp2"] == 3
        assert row["option_tracking"]["instrument_key"] == "NSE_FO|CI_TEST"
        assert row["actual_option_costs"]["total_cost_rupees"] == 25.0

        excursion = store._run(
            "SELECT mfe_r, mae_r, bars_held, time_stop_review_due FROM signals WHERE id=%s",
            (signal_id,),
            "one",
        )
        assert excursion == (1.2, 0.4, 3, True)
    finally:
        if initialized:
            if signal_id is not None:
                store._run("DELETE FROM signals WHERE id=%s", (signal_id,))
            store._run(
                "DELETE FROM normal_setup_state WHERE trade_day=%s AND symbol=%s",
                (today, symbol),
            )
        store.close()
