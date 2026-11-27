from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
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


def test_persistently_stale_candle_does_not_trigger_unbounded_retries(monkeypatch, caplog):
    import logging

    _configure(monkeypatch)
    caplog.set_level(logging.WARNING, logger="engine")
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
    assert "ABC:stale:" in caplog.text


def test_prior_session_stale_candle_is_not_retried(monkeypatch):
    _configure(monkeypatch)
    now = datetime(2026, 11, 23, 10, 5, 6, tzinfo=IST)
    expected = engine_module.latest_eligible_bar(now)
    stale = pd.DataFrame(index=[expected - pd.Timedelta(days=1)])
    state = SimpleNamespace(info=SimpleNamespace(symbol="NIFTYFPI"), err="")

    engine = object.__new__(Engine)
    engine.client = SimpleNamespace(auth_failed=False)
    calls = []

    def load(_state, _now, _module):
        calls.append(1)
        return stale

    engine._load = load
    result = engine._load_many("normal", [state], now, force=False)

    assert len(calls) == 1
    assert result["NIFTYFPI"].index[-1] < expected

def test_option_liquidity_gate_defaults_off():
    env = os.environ.copy()
    env.pop("OPTION_LIQUIDITY_CHECKS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.config import OPTION_LIQUIDITY_CHECKS; print(OPTION_LIQUIDITY_CHECKS)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"


def test_scan_request_budget_defaults_are_bounded():
    env = os.environ.copy()
    env.pop("RATE_PER_SEC", None)
    env.pop("RATE_PER_MIN", None)
    env.pop("RATE_PER_30MIN", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.config import ENGINE; "
            "print(ENGINE.rate_per_sec, ENGINE.rate_per_min, ENGINE.rate_per_30min, ENGINE.retry_passes)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "15 450 1800 1"
