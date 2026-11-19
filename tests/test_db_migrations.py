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
    assert applied == [(1, "baseline"), (2, "telegram_bot_state"), (3, "time_stop_review_flag")]
    assert any(sql == MIGRATIONS[0][2] for sql, _, _ in calls)
    assert any(sql == MIGRATIONS[1][2] for sql, _, _ in calls)
    assert any(sql == MIGRATIONS[2][2] for sql, _, _ in calls)
    assert calls[-1][0].startswith("DELETE FROM signals")


def test_postgres_store_does_not_reapply_recorded_migrations():
    store, calls = _fake_store((1, 2, 3))
    store.init()

    assert not any(sql in (MIGRATIONS[0][2], MIGRATIONS[1][2], MIGRATIONS[2][2]) for sql, _, _ in calls)
    assert calls[-1][0].startswith("DELETE FROM signals")
