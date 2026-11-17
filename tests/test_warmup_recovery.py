from datetime import datetime, timedelta

import pandas as pd

from app.common import SymbolInfo
from app.config import IST
from app.db import MemoryStore
from app.engine import Engine, SymbolState


def test_empty_historical_warmup_retries_after_backoff():
    now = datetime(2026, 11, 17, 8, 40, tzinfo=IST)
    idx = pd.date_range("2026-11-13 09:15", periods=60, freq="5min", tz=IST)
    history = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0},
        index=idx,
    )

    class Client:
        def __init__(self):
            self.history_calls = 0

        def historical(self, key, from_date, to_date):
            self.history_calls += 1
            if self.history_calls == 1:
                return pd.DataFrame(columns=list(history.columns))
            return history

        def intraday(self, key):
            return pd.DataFrame(columns=list(history.columns))

    client = Client()
    engine = Engine(client, MemoryStore())
    state = SymbolState(SymbolInfo("TEST", "NSE_EQ|TEST", "normal", lot=1))

    # Empty historical response is not treated as a successful daily warm-up.
    engine._load(state, now, "normal")
    assert client.history_calls == 1
    assert state.warm_day is None
    assert state.warm_retry_at == now + timedelta(minutes=5)

    # A scan before the backoff expires must not hammer the history endpoint.
    engine._load(state, now + timedelta(minutes=2), "normal")
    assert client.history_calls == 1

    # Retry after the backoff and accept the recovered historical data.
    engine._load(state, now + timedelta(minutes=5), "normal")
    assert client.history_calls == 2
    assert state.warm_day == now.date()
    assert state.warm_retry_at is None
    assert len(state.hist) > 0
