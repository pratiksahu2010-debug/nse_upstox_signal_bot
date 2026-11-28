import threading

import pandas as pd
import pytest

from app.upstox import Budget, UpstoxClient, UpstoxError


class NoWaitBudget:
    def acquire(self):
        pass


class FakeResponse:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_candle_frame_sorts_deduplicates_and_keeps_optional_oi():
    candles = [
        ["2026-11-17T09:20:00+05:30", 101, 103, 100, 102, 50, 7],
        ["2026-11-17T09:15:00+05:30", 99, 101, 98, 100, 40],
        ["2026-11-17T09:20:00+05:30", 102, 104, 101, 103, 60, 8],
    ]
    frame = UpstoxClient._frame(candles)
    assert frame.index.is_monotonic_increasing
    assert len(frame) == 2
    assert frame.iloc[-1]["close"] == 103
    assert frame.iloc[-1]["oi"] == 8
    assert pd.isna(frame.iloc[0]["oi"])


def test_candle_frame_drops_malformed_and_impossible_ohlc_rows():
    candles = [
        ["2026-11-17T09:15:00+05:30", 100, 99, 98, 100, 10],  # high below open
        ["bad-time", 100, 101, 99, 100, 10],
        ["2026-11-17T09:25:00+05:30", 100, 102, 99, 101, 10],
        ["2026-11-17T09:30:00+05:30", 100, 102, 99, float("nan"), 10],
        ["2026-11-17T09:35:00+05:30", 100, 102, 99, 101, -1],
        ["short"],
    ]
    frame = UpstoxClient._frame(candles)
    assert len(frame) == 1
    assert frame.iloc[0]["close"] == 101


def test_auth_failure_is_latched_until_new_token_is_installed(monkeypatch):
    client = UpstoxClient("old-token", budget=NoWaitBudget())
    fake = FakeSession([
        FakeResponse(401, text="unauthorized"),
        FakeResponse(200, {"data": {"candles": []}}),
    ])
    monkeypatch.setattr(client, "_session", lambda: fake)
    with pytest.raises(UpstoxError, match="auth error 401"):
        client._get("/test")
    assert client.auth_failed is True
    with pytest.raises(UpstoxError, match="authentication is rejected"):
        client._get("/test")
    assert len(fake.calls) == 1

    client.set_token("new-token")
    assert client.auth_failed is False
    assert client._get("/test") == {"data": {"candles": []}}
    assert fake.calls[-1][1]["headers"]["Authorization"] == "Bearer new-token"


def test_rate_limit_retries_with_invalid_retry_after_fallback(monkeypatch):
    client = UpstoxClient("token", budget=NoWaitBudget())
    fake = FakeSession([
        FakeResponse(429, headers={"Retry-After": "not-a-number"}),
        FakeResponse(200, {"ok": True}),
    ])
    monkeypatch.setattr(client, "_session", lambda: fake)
    sleeps = []
    monkeypatch.setattr("app.upstox.time.sleep", lambda seconds: sleeps.append(seconds))
    assert client._get("/test", retries=2) == {"ok": True}
    assert len(fake.calls) == 2
    assert sleeps == [2.0]


def test_server_error_retries_but_bad_request_does_not(monkeypatch):
    client = UpstoxClient("token", budget=NoWaitBudget())
    fake = FakeSession([FakeResponse(503), FakeResponse(200, {"ok": True})])
    monkeypatch.setattr(client, "_session", lambda: fake)
    monkeypatch.setattr("app.upstox.time.sleep", lambda seconds: None)
    assert client._get("/test", retries=2) == {"ok": True}
    assert len(fake.calls) == 2

    bad = FakeSession([FakeResponse(400, text="bad request")])
    monkeypatch.setattr(client, "_session", lambda: bad)
    with pytest.raises(UpstoxError, match="400"):
        client._get("/test", retries=3)
    assert len(bad.calls) == 1


def test_invalid_json_is_retried_and_diagnostics_never_expose_token(monkeypatch):
    client = UpstoxClient("secret-token", budget=NoWaitBudget())
    fake = FakeSession([
        FakeResponse(200, ValueError("invalid json")),
        FakeResponse(200, {"ok": True}),
    ])
    monkeypatch.setattr(client, "_session", lambda: fake)
    monkeypatch.setattr("app.upstox.time.sleep", lambda seconds: None)
    assert client._get("/test", retries=2) == {"ok": True}
    diag = client.diagnostics()
    assert diag["token_configured"] is True
    assert "secret-token" not in repr(diag)
    assert diag["last_error"] == ""


def test_session_is_thread_local():
    client = UpstoxClient("token", budget=NoWaitBudget())
    sessions = []
    barrier = threading.Barrier(2)

    def worker():
        session = client._session()
        sessions.append(session)
        barrier.wait()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]



@pytest.mark.parametrize("latch_auth", [True, False])
def test_late_401_from_old_token_does_not_poison_rotated_token(monkeypatch, latch_auth):
    client = UpstoxClient("old-token", budget=NoWaitBudget())

    class RotatingSession:
        def get(self, url, **kwargs):
            # Simulate another worker installing a replacement token while
            # this request, authenticated with the old token, is in flight.
            client.set_token("new-token")
            return FakeResponse(401, text="old token rejected")

    monkeypatch.setattr(client, "_session", lambda: RotatingSession())
    with pytest.raises(UpstoxError, match="stale token rejected"):
        client._get("/test", latch_auth=latch_auth)

    assert client.auth_failed is False
    assert client.diagnostics()["token_configured"] is True
    assert client._token == "new-token"
    assert client.auth_status is None
    assert client._ltp_auth_disabled is False
    assert client._optional_data_disabled == set()


def test_vectorized_candle_frame_preserves_validation_and_timestamp_semantics():
    candles = [
        ["2026-11-17T03:45:00Z", "100", "102", "99", "101", "10", None],
        ["2026-11-17T03:50:00Z", 101, 103, 100, 102, 20, "bad-oi"],
        ["not-a-timestamp", 101, 103, 100, 102, 20, 5],
        ["2026-11-17T04:00:00Z", 101, 103, 100, 102, float("inf"), 5],
        ["2026-11-17T03:55:00Z", 101, 103, 100, 102, 30, 7],
        ["2026-11-17T04:05:00Z", 101, 103, 100, 102, 40, "nan"],
    ]

    frame = UpstoxClient._frame(candles)

    assert len(frame) == 3
    assert frame.index.is_monotonic_increasing
    assert str(frame.index.tz) == "UTC+05:30"
    assert frame.iloc[0]["close"] == 101
    assert pd.isna(frame.iloc[0]["oi"])
    assert frame.iloc[1]["close"] == 102
    assert frame.iloc[1]["oi"] == 7
    assert pd.isna(frame.iloc[2]["oi"])



def test_rate_limit_cooldown_is_shared_by_all_budget_callers(monkeypatch):
    clock = {"now": 100.0}
    sleeps = []

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr("app.upstox.time.monotonic", monotonic)
    monkeypatch.setattr("app.upstox.time.sleep", sleep)

    budget = Budget(10, 100, 1000)
    budget.defer(2.0)

    # A different worker's next request must observe the same server cooldown.
    budget.acquire()
    assert sleeps == [2.0]
    assert budget.usage()["total"] == 2


def test_429_applies_shared_cooldown_before_retry(monkeypatch):
    clock = {"now": 200.0}
    sleeps = []

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr("app.upstox.time.monotonic", monotonic)
    monkeypatch.setattr("app.upstox.time.sleep", sleep)

    budget = Budget(10, 100, 1000)
    client = UpstoxClient("token", budget=budget)
    fake = FakeSession([
        FakeResponse(429, headers={"Retry-After": "3"}),
        FakeResponse(200, {"ok": True}),
    ])
    monkeypatch.setattr(client, "_session", lambda: fake)

    assert client._get("/test", retries=2) == {"ok": True}
    assert sleeps == [3.0]
    assert budget.usage()["total"] == 2
