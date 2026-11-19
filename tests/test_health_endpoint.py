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
