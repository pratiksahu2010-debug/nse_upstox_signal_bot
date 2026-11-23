from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from app import engine as engine_module
from app.engine import Engine


IST = timezone(timedelta(hours=5, minutes=30))


def _configure(monkeypatch):
    monkeypatch.setattr(
        engine_module,
        "ENGINE",
        SimpleNamespace(
            candle_min=5,
            fetch_delay_sec=6,
            workers=2,
            retry_passes=1,
        ),
    )
    monkeypatch.setattr(engine_module._time, "sleep", lambda _seconds: None)


def test_stale_candle_refresh_is_bounded_to_one_retry(monkeypatch):
    _configure(monkeypatch)
    now = datetime(2026, 11, 23, 10, 5, 6, tzinfo=IST)
    expected = engine_module.latest_eligible_bar(now)
    stale = pd.DataFrame(index=[expected - pd.Timedelta(minutes=5)])
    fresh = pd.DataFrame(index=[expected])
    state = SimpleNamespace(info=SimpleNamespace(symbol="ABC"), err="")

    engine = object.__new__(Engine)
    engine.client = SimpleNamespace(auth_failed=False)
    calls = []

    def load(_state, _now, _module):
        calls.append(1)
        return stale if len(calls) == 1 else fresh

    engine._load = load
    result = engine._load_many("normal", [state], now, force=False)

    assert len(calls) == 2
    assert result["ABC"].index[-1] == expected


def test_persistently_stale_candle_does_not_trigger_unbounded_retries(monkeypatch):
    _configure(monkeypatch)
    now = datetime(2026, 11, 23, 10, 5, 6, tzinfo=IST)
    expected = engine_module.latest_eligible_bar(now)
    stale = pd.DataFrame(index=[expected - pd.Timedelta(minutes=5)])
    state = SimpleNamespace(info=SimpleNamespace(symbol="ABC"), err="")

    engine = object.__new__(Engine)
    engine.client = SimpleNamespace(auth_failed=False)
    calls = []

    def load(_state, _now, _module):
        calls.append(1)
        return stale

    engine._load = load
    result = engine._load_many("normal", [state], now, force=False)

    assert len(calls) == 2
    assert result["ABC"].index[-1] < expected
