from types import SimpleNamespace

import pandas as pd

from app.config import IST
from app.engine import Engine, SymbolState
from app.upstox import UpstoxClient


class _Store:
    kind = "memory"


class _Budget:
    def acquire(self):
        return None


class _Response:
    status_code = 200
    headers = {}
    text = ""

    @staticmethod
    def json():
        return {"data": {"candles": []}}


class _Session:
    @staticmethod
    def get(*args, **kwargs):
        return _Response()


class _WarmupFailingClient:
    auth_failed = False

    def __init__(self, frame):
        self.frame = frame

    def historical(self, *args, **kwargs):
        raise RuntimeError("temporary historical endpoint failure")

    def intraday(self, *args, **kwargs):
        return self.frame


def test_historical_warmup_failure_does_not_skip_intraday_fetch():
    now = pd.Timestamp("2026-11-23 10:16:06", tz=IST).to_pydatetime()
    index = pd.date_range(end="2026-11-23 10:10:00", periods=60, freq="5min", tz=IST)
    frame = pd.DataFrame(
        {
            "open": range(100, 160),
            "high": range(101, 161),
            "low": range(99, 159),
            "close": range(100, 160),
            "volume": [1000] * 60,
        },
        index=index,
    )
    client = _WarmupFailingClient(frame)
    engine = Engine(client, _Store())
    state = SymbolState(SimpleNamespace(key="NSE_EQ|TEST", symbol="TEST"))

    loaded = engine._load(state, now, "normal")

    assert len(loaded) == 60
    assert loaded.index[-1] == index[-1]
    assert state.warm_day is None
    assert state.warm_retry_at is not None


def test_upstox_diagnostics_count_http_status_codes():
    client = UpstoxClient(token="test-token", budget=_Budget())
    client._session = lambda: _Session()

    payload = client._get("/test", retries=1)

    assert payload == {"data": {"candles": []}}
    assert client.diagnostics()["status_counts"] == {"200": 1}


from app.scanner_lease import ScannerLease


class _ContendedPostgresStore:
    kind = "postgres"

    def _run(self, sql, params=None, fetch=None):
        if "INSERT INTO scanner_leases" in sql:
            return None
        return None


def test_scanner_lease_reports_another_instance_as_standby():
    lease = ScannerLease(_ContendedPostgresStore())

    assert lease.acquire() is False
    assert lease.contended is True


class _UnavailablePostgresStore:
    kind = "postgres"

    def _run(self, *args, **kwargs):
        raise RuntimeError("database unavailable")


def test_scanner_lease_database_error_is_not_misreported_as_standby():
    lease = ScannerLease(_UnavailablePostgresStore())

    assert lease.acquire() is False
    assert lease.contended is False


def test_rejected_auth_skips_intraday_request_after_warmup_failure():
    now = pd.Timestamp("2026-11-23 10:16:06", tz=IST).to_pydatetime()
    index = pd.date_range(end="2026-11-23 10:10:00", periods=60, freq="5min", tz=IST)
    frame = pd.DataFrame(
        {
            "open": range(100, 160),
            "high": range(101, 161),
            "low": range(99, 159),
            "close": range(100, 160),
            "volume": [1000] * 60,
        },
        index=index,
    )

    class RejectedAuthClient(_WarmupFailingClient):
        auth_failed = False

        def __init__(self):
            self.intraday_calls = 0

        def historical(self, *args, **kwargs):
            self.auth_failed = True
            raise RuntimeError("auth error 401")

        def intraday(self, *args, **kwargs):
            self.intraday_calls += 1
            return frame

    client = RejectedAuthClient()
    engine = Engine(client, _Store())
    state = SymbolState(SimpleNamespace(key="NSE_EQ|TEST", symbol="TEST"))

    import pytest
    with pytest.raises(RuntimeError, match="auth error 401"):
        engine._load(state, now, "normal")

    assert client.intraday_calls == 0
