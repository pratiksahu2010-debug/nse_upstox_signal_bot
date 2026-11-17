from app import web


def test_health_is_immediate_and_does_not_initialize_application(monkeypatch):
    def fail_if_called():
        raise AssertionError("health must not trigger application initialization")

    monkeypatch.setattr(web, "_init", fail_if_called)
    response = web.app.test_client().get("/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert "time" in payload
    assert "initialized" in payload


def test_root_liveness_does_not_initialize_application(monkeypatch):
    def fail_if_called():
        raise AssertionError("root liveness must not trigger application initialization")

    monkeypatch.setattr(web, "_init", fail_if_called)
    response = web.app.test_client().get("/")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
