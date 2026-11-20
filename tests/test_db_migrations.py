from app.db import MIGRATIONS, PostgresStore


def _fake_store(applied_versions=()):
    store = object.__new__(PostgresStore)
    calls = []

    def fake_run(sql, params=None, fetch="none"):
        calls.append((sql, params, fetch))
        if sql == "SELECT version FROM schema_migrations":
            return [(version,) for version in applied_versions]
        return None

    store._run = fake_run
    return store, calls


def test_postgres_store_applies_each_missing_schema_migration_once():
    store, calls = _fake_store()
    store.init()

    applied = [
        params for sql, params, _ in calls
        if sql.startswith("INSERT INTO schema_migrations")
    ]
    assert applied == [(1, "baseline"), (2, "telegram_bot_state"), (3, "time_stop_review_flag"), (4, "training_feedback_labels"), (5, "first_target_hit"), (6, "time_to_target_metrics"), (7, "option_premium_and_actual_costs")]
    assert any(sql == MIGRATIONS[0][2] for sql, _, _ in calls)
    assert any(sql == MIGRATIONS[1][2] for sql, _, _ in calls)
    assert any(sql == MIGRATIONS[2][2] for sql, _, _ in calls)
    assert any(sql == MIGRATIONS[3][2] for sql, _, _ in calls)
    assert any(sql == MIGRATIONS[4][2] for sql, _, _ in calls)
    assert any(sql == MIGRATIONS[5][2] for sql, _, _ in calls)
    assert any(sql == MIGRATIONS[6][2] for sql, _, _ in calls)
    # Existing installations may have a version-only migration ledger.
    # Bootstrap must add/backfill the metadata columns before reading or writing it.
    sql_calls = [sql for sql, _, _ in calls]
    add_name = next(i for i, sql in enumerate(sql_calls) if "ADD COLUMN IF NOT EXISTS name TEXT" in sql)
    add_applied_at = next(i for i, sql in enumerate(sql_calls) if "ADD COLUMN IF NOT EXISTS applied_at" in sql)
    backfill = next(i for i, sql in enumerate(sql_calls) if "UPDATE schema_migrations SET name" in sql)
    enforce_not_null = next(i for i, sql in enumerate(sql_calls) if "ALTER COLUMN name SET NOT NULL" in sql)
    read_versions = sql_calls.index("SELECT version FROM schema_migrations")
    assert add_name < backfill < enforce_not_null < read_versions
    assert add_applied_at < read_versions
    schema_reconcile = next(i for i, sql in enumerate(sql_calls) if sql == MIGRATIONS[0][2])
    assert "UPDATE signals" in sql_calls[schema_reconcile]
    assert "SET trade_day = (bar_ts AT TIME ZONE 'Asia/Kolkata')::date" in sql_calls[schema_reconcile]
    assert "ADD COLUMN IF NOT EXISTS trade_day DATE" in sql_calls[schema_reconcile]
    assert "RENAME COLUMN bar_time TO bar_ts" in sql_calls[schema_reconcile]
    assert sql_calls[schema_reconcile].index("RENAME COLUMN bar_time TO bar_ts") < sql_calls[schema_reconcile].index("SET trade_day = (bar_ts AT TIME ZONE")
    assert not any(sql.lstrip().upper().startswith("DELETE FROM SIGNALS") for sql, _, _ in calls)
    assert "ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'OPEN'" in sql_calls[schema_reconcile]


def test_postgres_store_does_not_reapply_recorded_migrations():
    store, calls = _fake_store((1, 2, 3, 4, 5, 6, 7))
    store.init()

    # The baseline SCHEMA is intentionally reconciled once on every startup,
    # even when migration 1 is recorded, to repair columns missing from legacy
    # installations. This is not a migration re-application.
    assert sum(sql == MIGRATIONS[0][2] for sql, _, _ in calls) == 1
    # Versioned migrations 2-7 must not run again once recorded.
    assert not any(
        sql == migration_sql
        for sql, _, _ in calls
        for _, _, migration_sql in MIGRATIONS[1:]
    )
    assert not any(
        sql.startswith("INSERT INTO schema_migrations")
        for sql, _, _ in calls
    )
    assert not any(sql.lstrip().upper().startswith("DELETE FROM SIGNALS") for sql, _, _ in calls)
