import pytest

import app.db as db
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
    assert sorted(applied) == sorted([
        (1, "baseline"),
        (2, "telegram_bot_state"),
        (3, "time_stop_review_flag"),
        (4, "training_feedback_labels"),
        (5, "first_target_hit"),
        (6, "time_to_target_metrics"),
        (7, "option_premium_and_actual_costs"),
        (8, "schema_reconciliation"),
    ])
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
    assert "ADD COLUMN IF NOT EXISTS id BIGSERIAL" in sql_calls[schema_reconcile]
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_id ON signals (id)" in sql_calls[schema_reconcile]
    assert "DO $identity_compat$" in sql_calls[schema_reconcile]
    assert "SET DEFAULT md5(random()::text || clock_timestamp()::text)" in sql_calls[schema_reconcile]
    assert "WHERE \"identity\" IS NULL" in sql_calls[schema_reconcile]


def test_postgres_store_does_not_reapply_recorded_migrations():
    store, calls = _fake_store((1, 2, 3, 4, 5, 6, 7, 8))
    store.init()

    # The expensive compatibility reconciliation is versioned and must not
    # run again after version 8 has been recorded.
    assert sum(sql == MIGRATIONS[0][2] for sql, _, _ in calls) == 0
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



def test_postgres_store_reconciles_legacy_baseline_once():
    store, calls = _fake_store((1, 2, 3, 4, 5, 6, 7))
    store.init()

    assert sum(sql == MIGRATIONS[0][2] for sql, _, _ in calls) == 1
    assert any(
        params == (8, "schema_reconciliation")
        for sql, params, _ in calls
        if sql.startswith("INSERT INTO schema_migrations")
    )


def test_make_store_closes_pool_when_schema_initialization_fails(monkeypatch):
    class BrokenStore:
        closed = False

        def __init__(self, _dsn):
            pass

        def init(self):
            raise RuntimeError("schema migration failed")

        def close(self):
            self.closed = True

    broken = BrokenStore("postgresql://test")
    monkeypatch.setattr(db, "PostgresStore", lambda _dsn: broken)

    with pytest.raises(RuntimeError, match="schema migration failed"):
        db.make_store("postgresql://test")

    assert broken.closed is True


def test_postgres_pool_avoids_unsupported_startup_options_for_neon_pooler(monkeypatch):
    import psycopg2.pool

    captured = {}

    class FakePool:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(psycopg2.pool, "ThreadedConnectionPool", FakePool)
    PostgresStore("postgresql://example")

    assert captured["kwargs"]["connect_timeout"] == 10
    assert "options" not in captured["kwargs"]


def test_postgres_migration_init_holds_transaction_lock_for_entire_schema():
    events = []

    class FakeCursor:
        def __init__(self):
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            events.append((sql, params))
            if sql == "SELECT pg_try_advisory_xact_lock(%s)":
                self.result = (True,)

        def fetchone(self):
            return self.result

    class FakeConnection:
        closed = 0

        def cursor(self):
            return FakeCursor()

        def commit(self):
            events.append(("COMMIT", None))

        def rollback(self):
            events.append(("ROLLBACK", None))

    class FakePool:
        def __init__(self):
            self.connection = FakeConnection()
            self.returned = None

        def getconn(self):
            return self.connection

        def putconn(self, conn, close=False):
            self.returned = (conn, close)

    store = object.__new__(PostgresStore)
    store.pool = FakePool()

    def fake_init_schema():
        assert store._migration_connection is store.pool.connection
        with store._migration_connection.cursor() as cur:
            cur.execute("MIGRATION_SQL")

    store._init_schema = fake_init_schema
    store.init()

    statements = [sql for sql, _ in events]
    assert statements[:4] == [
        "SET LOCAL statement_timeout = 120000",
        "SET LOCAL lock_timeout = 15000",
        "SET LOCAL idle_in_transaction_session_timeout = 60000",
        "SELECT pg_try_advisory_xact_lock(%s)",
    ]
    assert statements.index("MIGRATION_SQL") < statements.index("COMMIT")
    assert "SELECT pg_advisory_unlock(%s)" not in statements
    assert store.pool.returned == (store.pool.connection, False)


def test_postgres_migration_init_fails_fast_when_another_instance_holds_transaction_lock():
    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params=None):
            pass

        def fetchone(self):
            return (False,)

    class FakeConnection:
        closed = 0

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    class FakePool:
        def __init__(self):
            self.connection = FakeConnection()
            self.returned = None

        def getconn(self):
            return self.connection

        def putconn(self, conn, close=False):
            self.returned = (conn, close)

    store = object.__new__(PostgresStore)
    store.pool = FakePool()
    store._init_schema = lambda: (_ for _ in ()).throw(AssertionError("migration must not run"))

    with pytest.raises(RuntimeError, match="another application instance"):
        store.init()

    assert store.pool.returned == (store.pool.connection, False)
