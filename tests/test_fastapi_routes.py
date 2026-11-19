from fastapi import FastAPI

from app import web


def test_fastapi_app_exposes_required_routes():
    assert isinstance(web.app, FastAPI)
    paths = {route.path for route in web.app.routes}
    assert {
        "/", "/health", "/status", "/diag", "/diagnostics", "/signals",
        "/stats", "/symbols", "/config", "/pause", "/resume", "/force",
        "/test-telegram",
    } <= paths


def test_safe_config_never_exposes_secrets():
    payload = web._safe_config()
    serialized = str(payload).lower()
    assert "token" not in serialized
    assert "password" not in serialized
    assert payload["signal_only"] is True
    assert payload["orders_enabled"] is False
