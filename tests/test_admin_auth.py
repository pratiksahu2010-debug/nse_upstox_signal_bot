from app import web


def test_admin_auth_fails_closed_when_secret_is_unset(monkeypatch):
    monkeypatch.setattr(web, "ADMIN_TOKEN", "")
    with web.app.test_request_context("/status"):
        assert web._auth() is False


def test_admin_auth_accepts_bearer_header(monkeypatch):
    monkeypatch.setattr(web, "ADMIN_TOKEN", "long-random-admin-secret")
    with web.app.test_request_context(
        "/status", headers={"Authorization": "Bearer long-random-admin-secret"}
    ):
        assert web._auth() is True


def test_admin_auth_accepts_legacy_query_token(monkeypatch):
    monkeypatch.setattr(web, "ADMIN_TOKEN", "long-random-admin-secret")
    with web.app.test_request_context("/status?token=long-random-admin-secret"):
        assert web._auth() is True


def test_wrong_bearer_token_does_not_fall_back_to_query_token(monkeypatch):
    monkeypatch.setattr(web, "ADMIN_TOKEN", "long-random-admin-secret")
    with web.app.test_request_context(
        "/status?token=long-random-admin-secret",
        headers={"Authorization": "Bearer wrong-secret"},
    ):
        assert web._auth() is False
