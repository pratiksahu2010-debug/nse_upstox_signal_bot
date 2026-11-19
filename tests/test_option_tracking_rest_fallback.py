from datetime import datetime, timedelta

import pytest

from app import engine as engine_module
from app.config import IST
from app.db import MemoryStore
from app.engine import Engine
from app.options import initial_option_tracking


OPTION_KEY = "NSE_FO|TRACKED_OPTION"


class FakeStream:
    def __init__(self, quote=None):
        self._quote = quote

    def quote(self, _key):
        return self._quote


class FakeClient:
    def __init__(self, now, fail=False):
        self.now = now
        self.fail = fail
        self.calls = []

    def full_quotes(self, keys):
        self.calls.append(list(keys))
        if self.fail:
            raise RuntimeError("REST quote endpoint unavailable")
        return {
            OPTION_KEY: {
                "last_price": 12.0,
                "timestamp": self.now.timestamp(),
                "depth": {
                    "buy": [{"price": 11.9}],
                    "sell": [{"price": 12.1}],
                },
            }
        }


class TrackingStore:
    def __init__(self):
        self.updated = []

    def update_option_tracking(self, signal_id, tracking):
        self.updated.append((signal_id, tracking))


def _tracking(now):
    return initial_option_tracking(
        OPTION_KEY,
        {
            "bid": 9.9,
            "ask": 10.1,
            "ltp": 10.0,
            "quote_age_sec": 1,
            "quote_timestamp": (now - timedelta(seconds=1)).timestamp(),
        },
        max_quote_age_sec=20,
    )


@pytest.mark.parametrize("stream_quote", [None, "stale"])
def test_open_option_tracking_uses_batched_rest_fallback_for_missing_or_stale_stream_quote(
    monkeypatch, stream_quote
):
    now = datetime(2026, 11, 19, 10, 10, tzinfo=IST)
    client = FakeClient(now)
    store = TrackingStore()
    engine = Engine(client, store)
    engine.market_stream = FakeStream(
        None if stream_quote is None else {
            "ltp": 8.0,
            "exchange_ts_ms": int((now - timedelta(minutes=2)).timestamp() * 1000),
            "volume": 100,
            "oi": 1000,
            "bid": 7.9,
            "ask": 8.1,
        }
    )
    engine._stream_keys = [OPTION_KEY]
    engine._sync_market_stream_keys = lambda: None
    monkeypatch.setattr(engine_module, "OPTION_MAX_QUOTE_AGE_SEC", 20)

    row = {"id": 7, "option_tracking": _tracking(now)}
    engine._track_option_quotes({"RELIANCE": [row]}, now, {"rejects": {}})

    assert client.calls == [[OPTION_KEY]]
    assert len(store.updated) == 1
    signal_id, updated = store.updated[0]
    assert signal_id == 7
    assert updated["last_bid"] == 11.9
    assert updated["last_quote_timestamp"] == now.timestamp()
    assert updated["quote_samples"] == 2


def test_rest_fallback_failure_does_not_raise_or_overwrite_tracking(monkeypatch):
    now = datetime(2026, 11, 19, 10, 10, tzinfo=IST)
    client = FakeClient(now, fail=True)
    store = TrackingStore()
    engine = Engine(client, store)
    engine.market_stream = FakeStream()
    engine._stream_keys = [OPTION_KEY]
    engine._sync_market_stream_keys = lambda: None
    monkeypatch.setattr(engine_module, "OPTION_MAX_QUOTE_AGE_SEC", 20)

    row = {"id": 8, "option_tracking": _tracking(now)}
    engine._track_option_quotes({"RELIANCE": [row]}, now, {"rejects": {}})

    assert client.calls == [[OPTION_KEY]]
    assert store.updated == []
