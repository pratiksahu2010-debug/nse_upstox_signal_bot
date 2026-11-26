"""Real PostgreSQL persistence and migration integration coverage.

The CI job supplies an isolated disposable database through TEST_DATABASE_URL.
This deliberately starts from the production-era schema_migrations and signals
layouts so startup reconciliation is tested against the real legacy shape.
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.db import PostgresStore


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires isolated TEST_DATABASE_URL")
def test_postgres_migrations_and_signal_lifecycle_round_trip():
    store = PostgresStore(os.environ["TEST_DATABASE_URL"])
    signal_id = None
    legacy_id = None
    legacy_module_signal_id = None
    legacy_old_id = None
    initialized = False
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    stamp = datetime.now(timezone.utc)
    symbol = "CI_POSTGRES_INTEGRATION"
    legacy_symbol = "CI_LEGACY_BAR_TIME"
    legacy_old_symbol = "CI_LEGACY_OLD_HISTORY"
    old_stamp = stamp - timedelta(days=200)

    try:
        # Reproduce the old production migration ledger: version only, with
        # baseline version 1 already recorded even though its schema is stale.
        store._run(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)"
        )
        store._run("INSERT INTO schema_migrations (version) VALUES (1)")
        # Reproduce a retired production FK: the current bot does not use
        # signal_claims, but its legacy identity column once referenced it.
        store._run("CREATE TABLE signal_claims (identity TEXT PRIMARY KEY)")
        store._run(
            """
            CREATE TABLE signals (
                identity TEXT NOT NULL DEFAULT 'legacy-existing',
                entry_price DOUBLE PRECISION NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                bar_time TIMESTAMPTZ NOT NULL,
                entry DOUBLE PRECISION,
                sl DOUBLE PRECISION,
                tp1 DOUBLE PRECISION,
                tp2 DOUBLE PRECISION,
                score INTEGER,
                trig TEXT,
                details JSONB,
                r_result DOUBLE PRECISION,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (symbol, side, bar_time)
            )
            """
        )
        store._run(
            """
            INSERT INTO signals
                (symbol, side, bar_time, entry_price, entry, sl, tp1, tp2,
                 score, trig, details)
            VALUES
                (%s, 'LONG', %s, 100, 100, 98, 102, 104,
                 91, 'LEGACY_FIXTURE',
                 '{"indicator_snapshot":{"adx":27.0}}'::jsonb)
            """,
            (legacy_symbol, stamp),
        )
        store._run(
            """INSERT INTO signals (symbol, side, bar_time, entry_price, entry, sl, tp1, tp2)
               VALUES (%s, 'SHORT', %s, 200, 200, 202, 198, 196)""",
            (legacy_old_symbol, old_stamp),
        )
        # Keep existing legacy identity values valid while adding the FK
        # that caused production signal inserts to fail.
        store._run("INSERT INTO signal_claims(identity) VALUES ('legacy-existing')")
        store._run(
            "ALTER TABLE signals ADD CONSTRAINT signals_identity_fkey "
            "FOREIGN KEY (identity) REFERENCES signal_claims(identity)"
        )
        # Existing identity values are present, but the legacy column has no
        # default for new rows. Startup reconciliation must add one safely.
        store._run('ALTER TABLE signals ALTER COLUMN identity DROP DEFAULT')
        # Reproduce the legacy required signal_id column with no default.
        store._run("ALTER TABLE signals ADD COLUMN signal_id BIGINT")
        store._run(
            """UPDATE signals SET signal_id = numbered.row_number
               FROM (SELECT ctid, row_number() OVER (ORDER BY ctid) AS row_number
                     FROM signals) AS numbered
               WHERE signals.ctid = numbered.ctid"""
        )
        store._run("ALTER TABLE signals ALTER COLUMN signal_id SET NOT NULL")
        # Reproduce additional pre-repository lifecycle columns. The cycle ID
        # is backfilled/defaulted by startup reconciliation; price aliases are
        # populated from the current canonical SL/target values on each insert.
        store._run("ALTER TABLE signals ADD COLUMN state_cycle_id TEXT")
        store._run("ALTER TABLE signals ADD COLUMN stop_loss DOUBLE PRECISION")
        store._run("ALTER TABLE signals ADD COLUMN target1 DOUBLE PRECISION")
        store._run("ALTER TABLE signals ADD COLUMN target2 DOUBLE PRECISION")
        store._run("""UPDATE signals SET state_cycle_id='legacy-cycle-' || symbol,
            stop_loss=sl, target1=tp1, target2=tp2""")
        for legacy_column in ("state_cycle_id", "stop_loss", "target1", "target2"):
            store._run(f"ALTER TABLE signals ALTER COLUMN {legacy_column} SET NOT NULL")

        store.init()
        initialized = True

        # The scanner lease must also coordinate independent Render instances.
        # Keep the TTL deliberately short here so this real-Postgres test proves
        # renewal, not merely the in-memory fake's approximation of it.
        from app.scanner_lease import ScannerLease
        first_scanner = ScannerLease(store, lease_seconds=1, renew_seconds=0.1)
        second_scanner = ScannerLease(store, lease_seconds=1, renew_seconds=0.1)
        try:
            assert first_scanner.acquire() is True
            assert second_scanner.acquire() is False
            first_scanner.start_heartbeat()
            import time
            time.sleep(1.25)
            assert second_scanner.acquire() is False
            first_scanner.release()
            assert second_scanner.acquire() is True
        finally:
            first_scanner.release()
            second_scanner.release()

        # The Telegram leader lease must coordinate independent poller objects
        # using ordinary short PostgreSQL transactions (including PgBouncer).
        from app.telegram_polling import TelegramPoller
        first_poller = TelegramPoller(None, store, token="ci-token", chat_id="1")
        second_poller = TelegramPoller(None, store, token="ci-token", chat_id="1")
        try:
            assert first_poller._acquire_leader_lock() is True
            assert second_poller._acquire_leader_lock() is False
            assert first_poller._renew_leader_lock() is True
            first_poller._release_leader_lock()
            assert second_poller._acquire_leader_lock() is True
        finally:
            first_poller._release_leader_lock()
            second_poller._release_leader_lock()
        legacy_id = store._run(
            "SELECT id FROM signals WHERE symbol=%s AND side='LONG' AND bar_ts=%s",
            (legacy_symbol, stamp), "one",
        )[0]
        legacy_old_id = store._run(
            "SELECT id FROM signals WHERE symbol=%s AND side='SHORT' AND bar_ts=%s",
            (legacy_old_symbol, old_stamp), "one",
        )[0]
        assert legacy_id > 0 and legacy_old_id > 0 and legacy_id != legacy_old_id
        # The retired FK is removed, but existing identity data remains intact.
        assert store._run(
            """SELECT count(*) FROM pg_constraint
               WHERE conrelid='signals'::regclass
                 AND confrelid='signal_claims'::regclass AND contype='f'""",
            fetch="one",
        )[0] == 0
        # Existing non-null identities must remain unchanged.
        assert store._run(
            "SELECT identity FROM signals WHERE id=%s", (legacy_id,), "one"
        )[0] == "legacy-existing"
        identity_default = store._run(
            """SELECT column_default FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name='signals'
                 AND column_name='identity'""",
            fetch="one",
        )[0]
        assert identity_default and "md5" in identity_default

        # Re-running init must be safe and must not reapply completed migrations.
        store.init()
        versions = store._run("SELECT version FROM schema_migrations ORDER BY version", fetch="all")
        assert [row[0] for row in versions] == list(range(1, 10))

        # The legacy timestamp and signal row must survive the in-place rename.
        migrated = store._run(
            "SELECT bar_ts, trade_day FROM signals WHERE id=%s",
            (legacy_id,),
            "one",
        )
        assert migrated[0] == stamp
        assert migrated[1] == today
        assert store._run("SELECT status FROM signals WHERE id=%s", (legacy_id,), "one")[0] == "OPEN"
        # Startup must not prune historical signals merely because they are older than 180 days.
        old_row = store._run(
            "SELECT bar_ts, trade_day, status FROM signals WHERE id=%s",
            (legacy_old_id,), "one",
        )
        assert old_row == (old_stamp, old_stamp.astimezone(ZoneInfo("Asia/Kolkata")).date(), "OPEN")
        legacy_module = store._run(
            "SELECT module FROM signals WHERE id=%s", (legacy_id,), "one"
        )[0]
        assert legacy_module == "normal"
        legacy_training = next(
            item for item in store.training_rows(today) if item["id"] == legacy_id
        )
        assert legacy_training["snapshot"] == {"adx": 27.0}
        assert legacy_training["symbol"] == legacy_symbol
        assert legacy_training["module"] == "normal"

        # A pre-module unique key must not block a distinct module from
        # recording its own signal on the same symbol/candle.
        legacy_module_signal = {
            "module": "momentum",
            "symbol": legacy_symbol,
            "side": "LONG",
            "bar_ts": stamp,
            "trade_day": today,
            "entry": 100.0,
            "sl": 98.0,
            "tp1": 102.0,
            "tp2": 104.0,
            "score": 90,
            "trig": "CI_LEGACY_MODULE_DEDUPE",
            "details": {},
        }
        legacy_module_signal_id = store.add_signal(legacy_module_signal)
        assert legacy_module_signal_id is not None
        assert store._run("SELECT signal_id FROM signals WHERE id=%s",
                          (legacy_module_signal_id,), "one")[0] is not None
        new_identity = store._run(
            "SELECT identity FROM signals WHERE id=%s",
            (legacy_module_signal_id,), "one",
        )[0]
        assert new_identity and new_identity != "legacy-existing"
        legacy_cycle_id = store._run(
            "SELECT state_cycle_id FROM signals WHERE id=%s", (legacy_id,), "one"
        )[0]
        assert legacy_cycle_id == "legacy-cycle-" + legacy_symbol
        persisted_legacy_aliases = store._run(
            "SELECT entry_price, stop_loss, target1, target2, state_cycle_id FROM signals WHERE id=%s",
            (legacy_module_signal_id,), "one",
        )
        assert persisted_legacy_aliases[:4] == (100.0, 98.0, 102.0, 104.0)
        assert persisted_legacy_aliases[4]
        assert persisted_legacy_aliases[4] != legacy_cycle_id

        store.save_normal_state(today, symbol, {"phase": "TRIGGERED", "side": 1})
        assert store.load_normal_states(today)[symbol] == {"phase": "TRIGGERED", "side": 1}

        # Event-sourcing primitives use the same real PostgreSQL store and
        # enforce event-ID idempotency plus contiguous per-aggregate sequences.
        event_aggregate = "CI_EVENT_STORE"
        event_one = {
            "event_id": "ci-event-1", "aggregate_type": "symbol",
            "aggregate_id": event_aggregate, "sequence": 1,
            "event_type": "SETUP_DETECTED", "payload": {"side": "LONG"},
            "occurred_at": stamp,
        }
        event_two = dict(event_one, event_id="ci-event-2", sequence=2,
                         event_type="CONFIRMED", payload={"score": 84})
        event_gap = dict(event_one, event_id="ci-event-gap", sequence=4,
                         event_type="PUBLISHED")
        assert store.append_domain_event(event_one) is True
        assert store.append_domain_event(event_one) is False
        assert store.append_domain_event(event_gap) is False
        assert store.append_domain_event(event_two) is True
        replayed = store.load_domain_events("symbol", event_aggregate)
        assert [item["sequence"] for item in replayed] == [1, 2]
        assert replayed[1]["payload"] == {"score": 84}
        assert store.save_actor_snapshot("symbol", event_aggregate, 2, {"phase": "CONFIRMED"})
        assert store.save_actor_snapshot("symbol", event_aggregate, 1, {"phase": "IDLE"}) is False
        assert store.load_actor_snapshot("symbol", event_aggregate)["state"] == {"phase": "CONFIRMED"}

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
        # The CI database is disposable; remove only records created by this test.
        if legacy_id is not None:
            store._run("DELETE FROM signals WHERE id=%s", (legacy_id,))
        if legacy_old_id is not None:
            store._run("DELETE FROM signals WHERE id=%s", (legacy_old_id,))
        if legacy_module_signal_id is not None:
            store._run("DELETE FROM signals WHERE id=%s", (legacy_module_signal_id,))
        if initialized:
            if signal_id is not None:
                store._run("DELETE FROM signals WHERE id=%s", (signal_id,))
            store._run(
                "DELETE FROM normal_setup_state WHERE trade_day=%s AND symbol=%s",
                (today, symbol),
            )
            store._run("DELETE FROM domain_events WHERE aggregate_type=%s AND aggregate_id=%s",
                       ("symbol", "CI_EVENT_STORE"))
            store._run("DELETE FROM actor_snapshots WHERE aggregate_type=%s AND aggregate_id=%s",
                       ("symbol", "CI_EVENT_STORE"))
        store.close()
