from starlette.requests import Request

from app import web


def _request(path="/status", headers=None):
    headers = headers or {}
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path.split("?", 1)[0],
        "raw_path": path.split("?", 1)[0].encode(),
        "query_string": path.split("?", 1)[1].encode() if "?" in path else b"",
        "headers": raw_headers,
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })


def test_admin_auth_fails_closed_when_secret_is_unset(monkeypatch):
    monkeypatch.setattr(web, "ADMIN_TOKEN", "")
    assert web._auth(_request()) is False


def test_admin_auth_accepts_bearer_header(monkeypatch):
    monkeypatch.setattr(web, "ADMIN_TOKEN", "long-random-admin-secret")
    assert web._auth(_request(headers={"Authorization": "Bearer long-random-admin-secret"})) is True


def test_admin_auth_accepts_legacy_query_token(monkeypatch):
    monkeypatch.setattr(web, "ADMIN_TOKEN", "long-random-admin-secret")
    assert web._auth(_request("/status?token=long-random-admin-secret")) is True


def test_wrong_bearer_token_does_not_fall_back_to_query_token(monkeypatch):
    monkeypatch.setattr(web, "ADMIN_TOKEN", "long-random-admin-secret")
    req = _request(
        "/status?token=long-random-admin-secret",
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert web._auth(req) is False
