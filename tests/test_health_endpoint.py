from fastapi.testclient import TestClient

from app import web


def _ready_engine():
    class FakeClient:
        auth_failed = False
        last_success_at = web.datetime.now(web.IST).isoformat(timespec="seconds")

        def diagnostics(self):
            return {"last_success_at": self.last_success_at}

    class FakeFeed:
        def stop(self):
            pass

        def diagnostics(self):
            return {
                "connected": True,
                "thread_alive": True,
                "subscribed_symbols": 150,
                "last_quote_age_sec": 5.0,
            }

    class FakeUniverse:
        ready = True
        built_on = web.datetime.now(web.IST).strftime("%Y-%m-%d")
        configured_symbols = [f"SYMBOL{i}" for i in range(150)]
        normal = [type("Item", (), {"symbol": f"SYMBOL{i}"})() for i in range(150)]

    class FakeEngine:
        client = FakeClient()
        uni = FakeUniverse()
        market_stream = FakeFeed()
        last_error = ""
        last_cycle_started_at = web.datetime.now(web.IST).isoformat(timespec="seconds")
        last_cycle_completed_at = last_cycle_started_at
        last_successful_cycle_at = last_cycle_started_at
        last_cycle_error = ""
        last_cycle_result = {"normal": {"scope": "full", "symbols": 150, "loaded": 150, "fresh": 150, "signals": 0}}

    return FakeEngine()


def _configure_ready(monkeypatch):
    from datetime import datetime

    class RunningThread:
        def is_alive(self):
            return True

    class RunningStop:
        thread = RunningThread()

        def set(self):
            pass

    class RunningPoller:
        running = True
        leader_lock_acquired = True

        def stop(self):
            pass
        thread = RunningThread()
        auth_failed = False
        last_success_at = datetime.now(web.IST).isoformat(timespec="seconds")

    monkeypatch.setattr(web.config, "RUN_SCHEDULER", True)
    monkeypatch.setattr(web.config, "UPSTOX_TOKEN", "configured-upstox-token")
    monkeypatch.setattr(web.config, "ADMIN_TOKEN", "configured-admin-token")
    monkeypatch.setattr(web.config, "TELEGRAM_TOKEN", "configured-telegram-token")
    monkeypatch.setattr(web.config, "TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setattr(web, "_scheduler_stop", RunningStop())
    monkeypatch.setattr(web, "_telegram_poller", RunningPoller())


def test_health_is_immediate_and_does_not_initialize_application(monkeypatch):
    def fail_if_called():
        raise AssertionError("health must not trigger application initialization")

    monkeypatch.setattr(web, "_init", fail_if_called)
    with TestClient(web.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert "time" in payload
    assert "initialized" in payload


def test_root_liveness_does_not_initialize_application(monkeypatch):
    monkeypatch.setattr(web, "_init", lambda: None)
    with TestClient(web.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_readiness_is_unavailable_before_application_initialization(monkeypatch):
    monkeypatch.setattr(web, "_started", False)
    monkeypatch.setattr(web, "engine", None)
    monkeypatch.setattr(web, "store", None)
    monkeypatch.setattr(web, "_init", lambda: (_ for _ in ()).throw(AssertionError("must not initialize")))

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "application_not_initialized"}


def test_readiness_accepts_initialized_memory_store_when_persistence_is_optional(monkeypatch):
    _freeze_market_time(monkeypatch)
    monkeypatch.setattr(web.config, "UPSTOX_STREAM_ENABLED", True)
    class FakeStore:
        kind = "memory"

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", FakeStore())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert payload["scheduler"] == {"enabled": True, "running": True}
    assert payload["upstox"]["token_configured"] is True
    assert payload["upstox"]["auth_failed"] is False
    assert payload["upstox"]["last_success_at"] is not None
    assert payload["telegram"]["credentials_configured"] is True
    assert payload["telegram"]["poller_running"] is True
    assert payload["telegram"]["auth_failed"] is False
    assert payload["database"] == {"kind": "memory", "persistent": False}


def test_readiness_rejects_disabled_full_quotes_when_liquidity_gate_is_required(monkeypatch):
    _freeze_market_time(monkeypatch)
    monkeypatch.setattr(web.config, "UPSTOX_STREAM_ENABLED", True)
    monkeypatch.setattr(web.config, "OPTION_LIQUIDITY_CHECKS", True)

    class FakeStore:
        kind = "memory"

    _configure_ready(monkeypatch)
    engine = _ready_engine()
    engine.client.diagnostics = lambda: {
        "last_success_at": engine.client.last_success_at,
        "optional_data_disabled": ["full_quotes"],
    }
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", engine)
    monkeypatch.setattr(web, "store", FakeStore())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "option_quote_endpoint_unavailable"}


def test_readiness_allows_telegram_standby_during_rolling_deploy(monkeypatch):
    _freeze_market_time(monkeypatch)
    monkeypatch.setattr(web.config, "UPSTOX_STREAM_ENABLED", True)
    class FakeStore:
        kind = "memory"

    _configure_ready(monkeypatch)
    web._telegram_poller.leader_lock_acquired = False
    web._telegram_poller.last_error = "standby: another instance owns Telegram polling"
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", FakeStore())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["telegram"]["leader"] is False
    assert response.json()["telegram"]["state"] == "standby"


def test_readiness_rejects_memory_store_when_persistent_database_is_required(monkeypatch):
    class FakeStore:
        kind = "memory"

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", FakeStore())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", True)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "persistent_database_required"}


def test_readiness_checks_postgres_connectivity(monkeypatch):
    class FakeStore:
        kind = "postgres"

        def _run(self, sql, fetch):
            assert sql == "SELECT 1"
            assert fetch == "one"
            raise RuntimeError("database offline")

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", FakeStore())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", True)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "database_unavailable"}


def test_readiness_rejects_missing_upstox_token(monkeypatch):
    class FakeStore:
        kind = "postgres"

        def _run(self, sql, fetch):
            return (1,)

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web.config, "UPSTOX_TOKEN", "")
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", FakeStore())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", True)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "upstox_token_missing"}


def test_readiness_rejects_missing_telegram_configuration(monkeypatch):
    class FakeStore:
        kind = "postgres"

        def _run(self, sql, fetch):
            return (1,)

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web.config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", FakeStore())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", True)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "telegram_not_configured"}


def test_readiness_rejects_upstox_auth_failure(monkeypatch):
    class FakeStore:
        kind = "postgres"

        def _run(self, sql, fetch):
            return (1,)

    class FakeClient:
        auth_failed = True

    class FakeEngine:
        client = FakeClient()
        uni = None
        last_error = ""

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", FakeEngine())
    monkeypatch.setattr(web, "store", FakeStore())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", True)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "upstox_auth_rejected"}


def test_readiness_rejects_incomplete_universe(monkeypatch):
    class FakeStore:
        kind = "postgres"

        def _run(self, sql, fetch):
            return (1,)

    class FakeUniverse:
        ready = False
        configured_symbols = ["NIFTY"]
        normal = []

    class FakeEngine:
        client = object()
        uni = FakeUniverse()
        last_error = ""

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", FakeEngine())
    monkeypatch.setattr(web, "store", FakeStore())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", True)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "universe_validation_failed"}



def test_readiness_rejects_stopped_scheduler_thread(monkeypatch):
    class StoppedThread:
        def is_alive(self):
            return False

    class StoppedScheduler:
        thread = StoppedThread()

        def set(self):
            pass

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web, "_scheduler_stop", StoppedScheduler())
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "scheduler_not_running"}


def test_readiness_rejects_telegram_auth_failure(monkeypatch):
    class RejectedPoller:
        running = True
        leader_lock_acquired = True
        auth_failed = True
        thread = type("Thread", (), {"is_alive": lambda self: True})()

        def stop(self):
            pass

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web, "_telegram_poller", RejectedPoller())
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "telegram_auth_rejected"}


def test_readiness_rejects_missing_telegram_poller(monkeypatch):
    _configure_ready(monkeypatch)
    monkeypatch.setattr(web, "_telegram_poller", None)
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "telegram_poller_not_running"}



def test_readiness_rejects_stale_telegram_connection(monkeypatch):
    from datetime import timedelta

    class StalePoller:
        running = True
        leader_lock_acquired = True
        auth_failed = False
        thread = type("Thread", (), {"is_alive": lambda self: True})()
        last_success_at = (web.datetime.now(web.IST) - timedelta(minutes=5)).isoformat()

        def stop(self):
            pass

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web, "_telegram_poller", StalePoller())
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "telegram_connection_stale"}



def test_readiness_rejects_missing_admin_token(monkeypatch):
    _configure_ready(monkeypatch)
    monkeypatch.setattr(web.config, "ADMIN_TOKEN", "")
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "admin_token_missing"}


def test_readiness_rejects_wrong_fixed_universe_count_even_if_flagged_ready(monkeypatch):
    class WrongCountUniverse:
        ready = True
        configured_symbols = ["NIFTY"]
        normal = [type("Item", (), {"symbol": "NIFTY"})()]

    class EngineWithWrongCount(_ready_engine().__class__):
        uni = WrongCountUniverse()

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", EngineWithWrongCount())
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "universe_validation_failed"}



def test_readiness_rejects_yesterdays_universe_during_weekday_session(monkeypatch):
    from datetime import datetime

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = cls(2026, 11, 20, 10, 0)
            return current.replace(tzinfo=tz) if tz else current

    class StaleUniverse:
        ready = True
        built_on = "2026-11-19"
        configured_symbols = [f"SYMBOL{i}" for i in range(150)]
        normal = [type("Item", (), {"symbol": f"SYMBOL{i}"})() for i in range(150)]

    class StaleEngine(_ready_engine().__class__):
        uni = StaleUniverse()

    _configure_ready(monkeypatch)
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", StaleEngine())
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)
    monkeypatch.setattr(web, "datetime", FrozenDateTime)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "universe_stale"}



def _freeze_market_time(monkeypatch, hour=10, minute=0):
    from datetime import datetime

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = cls(2026, 11, 20, hour, minute)
            return current.replace(tzinfo=tz) if tz else current

    monkeypatch.setattr(web, "datetime", FrozenDateTime)


def test_readiness_rejects_no_completed_scan_during_entry_window(monkeypatch):
    _freeze_market_time(monkeypatch)
    _configure_ready(monkeypatch)
    candidate = _ready_engine()
    candidate.last_cycle_completed_at = None
    candidate.last_successful_cycle_at = None
    candidate.last_cycle_result = {}
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", candidate)
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "scanner_cycle_not_yet_completed"}


def test_readiness_rejects_stale_scanner_cycle(monkeypatch):
    from datetime import datetime

    _freeze_market_time(monkeypatch)
    _configure_ready(monkeypatch)
    candidate = _ready_engine()
    old = datetime(2026, 11, 20, 9, 45, tzinfo=web.IST).isoformat()
    candidate.last_cycle_completed_at = old
    candidate.last_successful_cycle_at = old
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", candidate)
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "scanner_cycle_stale"}


def test_readiness_rejects_incomplete_150_symbol_scan(monkeypatch):
    _freeze_market_time(monkeypatch)
    _configure_ready(monkeypatch)
    candidate = _ready_engine()
    candidate.last_cycle_result = {
        "normal": {"scope": "full", "symbols": 150, "loaded": 120, "fresh": 120, "signals": 0}
    }
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", candidate)
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "scanner_data_incomplete"}


def test_readiness_rejects_stale_upstox_rest_data(monkeypatch):
    from datetime import datetime

    _freeze_market_time(monkeypatch)
    _configure_ready(monkeypatch)
    candidate = _ready_engine()
    candidate.client.last_success_at = datetime(2026, 11, 20, 9, 45, tzinfo=web.IST).isoformat()
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", candidate)
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "upstox_rest_data_stale"}


def test_readiness_rejects_disconnected_market_feed(monkeypatch):
    _freeze_market_time(monkeypatch)
    _configure_ready(monkeypatch)
    candidate = _ready_engine()
    candidate.market_stream.diagnostics = lambda: {
        "connected": False, "thread_alive": True, "subscribed_symbols": 150, "last_quote_age_sec": 1.0
    }
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", candidate)
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "market_feed_disconnected"}


def test_readiness_rejects_stale_market_feed_messages(monkeypatch):
    _freeze_market_time(monkeypatch)
    _configure_ready(monkeypatch)
    candidate = _ready_engine()
    candidate.market_stream.diagnostics = lambda: {
        "connected": True, "thread_alive": True, "subscribed_symbols": 150, "last_quote_age_sec": 180.0
    }
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", candidate)
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "market_feed_stale"}



def test_readiness_rejects_incomplete_market_feed_subscriptions(monkeypatch):
    _freeze_market_time(monkeypatch)
    _configure_ready(monkeypatch)
    candidate = _ready_engine()
    candidate.market_stream.diagnostics = lambda: {
        "connected": True,
        "thread_alive": True,
        "subscribed_symbols": 149,
        "last_quote_age_sec": 1.0,
    }
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", candidate)
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "market_feed_subscription_incomplete"}


def test_readiness_rejects_when_all_strategies_are_disabled(monkeypatch):
    _configure_ready(monkeypatch)
    monkeypatch.setattr(web.config, "ENABLE_NORMAL", False)
    monkeypatch.setattr(web.config, "ENABLE_MOMENTUM", False)
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "all_strategies_disabled"}



def test_readiness_requires_recent_cycle_after_last_entry_until_session_end(monkeypatch):
    _freeze_market_time(monkeypatch, hour=15, minute=5)
    _configure_ready(monkeypatch)
    candidate = _ready_engine()
    candidate.last_cycle_completed_at = None
    candidate.last_successful_cycle_at = None
    candidate.last_cycle_result = {}
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", candidate)
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "scanner_cycle_not_yet_completed"}


def test_readiness_accepts_completed_idle_cycle_after_last_entry(monkeypatch):
    _freeze_market_time(monkeypatch, hour=15, minute=5)
    _configure_ready(monkeypatch)
    candidate = _ready_engine()
    candidate.last_cycle_result = {
        "normal": {"scope": "idle", "symbols": 0, "loaded": 0, "signals": 0}
    }
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", candidate)
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True

    
def test_readiness_rejects_nonempty_but_stale_symbol_frames(monkeypatch):
    _freeze_market_time(monkeypatch)
    _configure_ready(monkeypatch)
    candidate = _ready_engine()
    candidate.last_cycle_result = {
        "normal": {"scope": "full", "symbols": 150, "loaded": 150, "fresh": 120, "signals": 0}
    }
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", candidate)
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "scanner_fresh_data_incomplete"}


def test_readiness_rejects_persistent_conflict_for_active_telegram_leader(monkeypatch):
    _configure_ready(monkeypatch)
    web._telegram_poller.last_error = "Telegram getUpdates conflict: another getUpdates request"
    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", _ready_engine())
    monkeypatch.setattr(web, "store", type("Store", (), {"kind": "memory"})())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "telegram_polling_conflict"}
