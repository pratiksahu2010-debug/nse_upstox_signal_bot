import time

from app.market_stream import UpstoxMarketStream


def test_wait_for_quotes_returns_only_fresh_exchange_ticks(monkeypatch):
    stream = UpstoxMarketStream.__new__(UpstoxMarketStream)
    now_ms = time.time() * 1000.0
    quotes = {
        "fresh": {"exchange_ts_ms": now_ms - 1_000, "ltp": 100.0},
        "stale": {"exchange_ts_ms": now_ms - 60_000, "ltp": 100.0},
        "missing": None,
    }
    monkeypatch.setattr(stream, "quote", lambda key: quotes.get(key))

    result = stream.wait_for_quotes(
        ["fresh", "stale", "missing"],
        max_age_sec=20,
        timeout_sec=0.01,
        poll_sec=0.005,
    )

    assert set(result) == {"fresh"}


def test_wait_for_quotes_does_not_accept_future_exchange_timestamp(monkeypatch):
    stream = UpstoxMarketStream.__new__(UpstoxMarketStream)
    future_ms = time.time() * 1000.0 + 5_000
    monkeypatch.setattr(
        stream, "quote",
        lambda key: {"exchange_ts_ms": future_ms, "ltp": 100.0},
    )

    result = stream.wait_for_quotes(
        ["future"], max_age_sec=20, timeout_sec=0.01, poll_sec=0.005,
    )

    assert result == {}
