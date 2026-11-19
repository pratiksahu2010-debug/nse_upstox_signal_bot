from fastapi.testclient import TestClient

from app import web


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

    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", object())
    monkeypatch.setattr(web, "store", FakeStore())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", False)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "initialized": True,
        "database": {"kind": "memory", "persistent": False},
    }


def test_readiness_rejects_memory_store_when_persistent_database_is_required(monkeypatch):
    class FakeStore:
        kind = "memory"

    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", object())
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

    monkeypatch.setattr(web, "_started", True)
    monkeypatch.setattr(web, "engine", object())
    monkeypatch.setattr(web, "store", FakeStore())
    monkeypatch.setattr(web.config, "REQUIRE_PERSISTENT_DB", True)

    with TestClient(web.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "reason": "database_unavailable"}
