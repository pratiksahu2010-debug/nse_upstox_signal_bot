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


class _FakeCursor:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.query = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.query = query

    def fetchone(self):
        return (self.acquired,)


class _FakeConnection:
    closed = 0

    def __init__(self, acquired=True):
        self.cur = _FakeCursor(acquired)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class _FakePool:
    def __init__(self, connection):
        self.connection = connection
        self.returned = []

    def getconn(self):
        return self.connection

    def putconn(self, conn, close=False):
        self.returned.append((conn, close))


def test_telegram_poller_holds_postgres_advisory_lock_until_release():
    from app.telegram_polling import TelegramPoller

    connection = _FakeConnection(acquired=True)
    pool = _FakePool(connection)
    store = type("Store", (), {
        "kind": "postgres", "pool": pool,
        "_run": staticmethod(lambda *args, **kwargs: None),
    })()
    poller = TelegramPoller(engine=None, store=store, token="test-token", chat_id="1")

    assert poller._acquire_leader_lock() is True
    assert poller.leader_lock_acquired is True
    assert poller._leader_conn is connection
    assert pool.returned == []

    poller._release_leader_lock()
    assert poller.leader_lock_acquired is False
    assert poller._leader_conn is None
    assert pool.returned == [(connection, False)]


def test_telegram_poller_standby_returns_connection_when_lock_is_taken():
    from app.telegram_polling import TelegramPoller

    connection = _FakeConnection(acquired=False)
    pool = _FakePool(connection)
    store = type("Store", (), {
        "kind": "postgres", "pool": pool,
        "_run": staticmethod(lambda *args, **kwargs: None),
    })()
    poller = TelegramPoller(engine=None, store=store, token="test-token", chat_id="1")

    assert poller._acquire_leader_lock() is False
    assert poller.leader_lock_acquired is False
    assert poller._leader_conn is None
    assert pool.returned == [(connection, False)]


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
