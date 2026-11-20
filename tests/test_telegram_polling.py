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


class _LeaseFakeStore:
    kind = "postgres"

    def __init__(self):
        self.owner = None
        self.queries = []

    def _run(self, sql, params=None, fetch="none"):
        self.queries.append((sql, params, fetch))
        if "INSERT INTO telegram_polling_leases" in sql:
            owner = params[1]
            if self.owner is None:
                self.owner = owner
                return (owner,)
            return None
        if "UPDATE telegram_polling_leases" in sql:
            owner = params[2]
            return (owner,) if self.owner == owner else None
        if "DELETE FROM telegram_polling_leases" in sql:
            if self.owner == params[1]:
                self.owner = None
        return None


def test_telegram_poller_lease_allows_only_one_postgres_consumer():
    from app.telegram_polling import TelegramPoller

    store = _LeaseFakeStore()
    first = TelegramPoller(engine=None, store=store, token="test-token", chat_id="1")
    second = TelegramPoller(engine=None, store=store, token="test-token", chat_id="1")

    assert first._acquire_leader_lock() is True
    assert first.leader_lock_acquired is True
    assert second._acquire_leader_lock() is False
    assert second.leader_lock_acquired is False
    assert first._renew_leader_lock() is True

    first._release_leader_lock()
    assert first.leader_lock_acquired is False
    assert second._acquire_leader_lock() is True
    second._release_leader_lock()
    assert store.owner is None


def test_telegram_poller_lease_renewal_fails_after_ownership_changes():
    from app.telegram_polling import TelegramPoller

    store = _LeaseFakeStore()
    poller = TelegramPoller(engine=None, store=store, token="test-token", chat_id="1")
    assert poller._acquire_leader_lock() is True

    # Simulate another process taking over after the lease expires.
    store.owner = "different-process"
    assert poller._renew_leader_lock() is False
    poller._release_leader_lock()
    assert store.owner == "different-process"


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


def test_telegram_sender_never_logs_bot_token_from_request_exception(monkeypatch, caplog):
    import logging
    import requests
    import app.telegram as telegram

    secret = "test-secret-bot-token"
    monkeypatch.setattr(telegram, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "TELEGRAM_TOKEN", secret)
    monkeypatch.setattr(telegram, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(telegram.time, "sleep", lambda *_: None)
    monkeypatch.setattr(telegram, "_last", [0.0])

    def fail(*args, **kwargs):
        raise requests.ConnectionError(
            f"failed URL https://api.telegram.org/bot{secret}/sendMessage"
        )

    monkeypatch.setattr(telegram.requests, "post", fail)
    with caplog.at_level(logging.ERROR, logger="telegram"):
        assert telegram.send("test message") is False

    assert secret not in caplog.text
    assert "ConnectionError" in caplog.text


def test_telegram_409_conflict_reason_is_classified_without_response_logging():
    from app.telegram_polling import _classify_telegram_conflict

    assert _classify_telegram_conflict(
        "Conflict: can't use getUpdates method while webhook is active"
    ) == "webhook_configured"
    assert _classify_telegram_conflict(
        "Conflict: terminated by other getUpdates request"
    ) == "another_getupdates_consumer"
    assert _classify_telegram_conflict("Conflict") == "unknown_conflict"
