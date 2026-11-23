import asyncio

from app.market_stream import UpstoxMarketStream


class _Client:
    def _get(self, path, retries=1):
        assert path == "/v3/feed/market-data-feed/authorize"
        return {"data": {"authorized_redirect_uri": "wss://example.invalid/feed?code=one-time"}}


class _Socket:
    def __init__(self, stream):
        self.stream = stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, payload):
        self.stream._stop.set()


def test_v3_websocket_uses_documented_accept_header(monkeypatch):
    import app.market_stream as market_stream

    stream = UpstoxMarketStream(_Client())
    stream.set_keys(["NSE_INDEX|Nifty 50"])
    captured = {}

    def fake_connect(uri, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        return _Socket(stream)

    monkeypatch.setattr(market_stream.websockets, "connect", fake_connect)

    asyncio.run(stream._run())

    assert captured["uri"].startswith("wss://")
    assert captured["extra_headers"] == {"Accept": "*/*"}
