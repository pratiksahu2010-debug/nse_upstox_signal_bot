"""Regression tests for optional Upstox LTP request isolation."""
import pytest

from app.upstox import UpstoxClient, UpstoxError


class NoopBudget:
    def acquire(self):
        pass


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.headers = {}
        self.text = "unauthorized"
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_optional_ltp_401_does_not_disable_candle_data(monkeypatch):
    client = UpstoxClient(token="test-token", budget=NoopBudget())
    session = FakeSession(FakeResponse(401))
    monkeypatch.setattr(client, "_session", lambda: session)

    assert client.ltp(["NSE_FO|TEST"]) == {}
    assert client.auth_failed is False
    assert client.auth_status is None
    assert "Api-Version" in session.calls[0][1]["headers"]
    assert session.calls[0][1]["headers"]["Api-Version"] == "2.0"

    # A subsequent candle-data authentication error still latches normally.
    with pytest.raises(UpstoxError, match="auth error 401"):
        client._get("/v3/historical-candle/test/minutes/5/2026-11-18/2026-11-01", retries=1)
    assert client.auth_failed is True


def test_ltp_success_uses_v2_header_and_returns_price(monkeypatch):
    client = UpstoxClient(token="test-token", budget=NoopBudget())
    session = FakeSession(FakeResponse(200, {
        "data": {
            "NSE_FO|TEST": {
                "last_price": 123.45,
                "instrument_token": "NSE_FO|TEST",
            }
        }
    }))
    monkeypatch.setattr(client, "_session", lambda: session)

    assert client.ltp(["NSE_FO|TEST"]) == {"NSE_FO|TEST": 123.45}
    assert client.auth_failed is False
    assert session.calls[0][1]["headers"]["Api-Version"] == "2.0"

def test_optional_ltp_auth_failure_opens_circuit_breaker(monkeypatch):
    client = UpstoxClient(token="test-token", budget=NoopBudget())
    session = FakeSession(FakeResponse(401))
    monkeypatch.setattr(client, "_session", lambda: session)

    assert client.ltp(["NSE_FO|TEST"]) == {}
    assert client.ltp(["NSE_FO|TEST"]) == {}
    assert len(session.calls) == 1
    assert client.auth_failed is False

    # Installing a replacement token re-enables the optional lookup.
    client.set_token("replacement-token")
    assert client.ltp(["NSE_FO|TEST"]) == {}
    assert len(session.calls) == 2
    assert client.auth_failed is False
