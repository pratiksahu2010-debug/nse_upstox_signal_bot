from fastapi.testclient import TestClient

from app import web


def _ready_engine():
    class FakeClient:
        auth_failed = False

    class FakeUniverse:
        ready = True
        configured_symbols = [f"SYMBOL{i}" for i in range(150)]
        normal = [object() for _ in range(150)]

    class FakeEngine:
        client = FakeClient()
        uni = FakeUniverse()
        last_error = ""

    return FakeEngine()


def _configure_ready(monkeypatch):
    monkeypatch.setattr(web.config, "RUN_SCHEDULER", True)
    monkeypatch.setattr(web.config, "UPSTOX_TOKEN", "configured-upstox-token")
    monkeypatch.setattr(web.config, "TELEGRAM_TOKEN", "configured-telegram-token")
    monkeypatch.setattr(web.config, "TELEGRAM_CHAT_ID", "123456")


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
    assert payload["scheduler"] == "enabled"
    assert payload["upstox"] == {"token_configured": True, "auth_failed": False}
    assert payload["telegram"] == {"credentials_configured": True}
    assert payload["database"] == {"kind": "memory", "persistent": False}


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
