from app.upstox import UpstoxClient


class NoopBudget:
    def acquire(self):
        pass


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"status": "success", "data": []}
        self.headers = {}
        self.text = "response"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_option_chain_uses_supported_endpoint_and_does_not_mutate_strategy():
    client = UpstoxClient(token="test-token", budget=NoopBudget())
    session = FakeSession(FakeResponse(payload={"status": "success", "data": [{"strike_price": 100}]}))
    client._session = lambda: session

    result = client.option_chain("NSE_INDEX|Nifty 50", "2026-11-26")

    assert result == [{"strike_price": 100}]
    url, kwargs = session.calls[0]
    assert url.endswith("/v2/option/chain")
    assert kwargs["params"] == {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "expiry_date": "2026-11-26",
    }
    assert kwargs["headers"]["Api-Version"] == "2.0"
    assert client.auth_failed is False


def test_full_quotes_splits_requests_at_upstox_500_key_limit():
    client = UpstoxClient(token="test-token", budget=NoopBudget())
    session = FakeSession(FakeResponse(payload={"status": "success", "data": {}}))
    client._session = lambda: session

    result = client.full_quotes([f"NSE_FO|{i}" for i in range(501)])

    assert result == {}
    assert len(session.calls) == 2
    assert len(session.calls[0][1]["params"]["instrument_key"].split(",")) == 500
    assert len(session.calls[1][1]["params"]["instrument_key"].split(",")) == 1


def test_optional_option_chain_auth_failure_does_not_disable_candle_auth():
    client = UpstoxClient(token="test-token", budget=NoopBudget())
    session = FakeSession(FakeResponse(status_code=401))
    client._session = lambda: session

    assert client.option_chain("NSE_INDEX|Nifty 50", "2026-11-26") == []
    assert client.auth_failed is False
    assert client.diagnostics()["optional_data_disabled"] == ["option_chain"]
    assert client.option_chain("NSE_INDEX|Nifty 50", "2026-11-26") == []
    assert len(session.calls) == 1
