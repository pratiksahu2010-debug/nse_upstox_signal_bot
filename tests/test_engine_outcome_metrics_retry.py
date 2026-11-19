from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from app import engine as engine_module
from app.common import SymbolInfo
from app.db import MemoryStore
from app.engine import Engine, SymbolState


class MetricsStore(MemoryStore):
    def __init__(self, fail_method=None):
        super().__init__()
        self.fail_method = fail_method
        self.outcome_calls = 0

    def update_excursions(self, *args, **kwargs):
        if self.fail_method == "update_excursions":
            raise RuntimeError("temporary excursion write failure")
        return super().update_excursions(*args, **kwargs)

    def update_target_times(self, *args, **kwargs):
        if self.fail_method == "update_target_times":
            raise RuntimeError("temporary target-time write failure")
        return super().update_target_times(*args, **kwargs)

    def update_outcome(self, *args, **kwargs):
        self.outcome_calls += 1
        return super().update_outcome(*args, **kwargs)


def _engine_with_open_signal(monkeypatch, store):
    now = datetime(2026, 11, 19, 10, 5, 10, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    signal_bar = pd.Timestamp("2026-11-19T09:55:00+05:30")
    current_bar = pd.Timestamp("2026-11-19T10:00:00+05:30")
    info = SymbolInfo("TEST", "NSE_EQ|TEST", "normal", underlying="TEST")
    row = {
        "module": "normal", "symbol": "TEST", "side": "LONG",
        "bar_ts": signal_bar.to_pydatetime(), "trade_day": now.date(),
        "entry": 100.0, "sl": 98.0, "tp1": 103.0, "tp2": 106.0,
        "score": 80, "trig": "PRIOR", "details": {"telegram_text": "prior"},
    }
    assert store.add_signal(row) is not None

    state = {
        "phase": "IDLE", "side": 0, "last_bar": current_bar.isoformat(),
        "cooldown_remaining": 0, "invalidated": False,
    }
    engine = Engine(client=object(), store=store, notify=lambda _message: True)
    engine.states = {("normal", "TEST"): SymbolState(info, normal_state=state)}
    engine.uni = SimpleNamespace(ready=True)

    index = pd.date_range(end=current_bar, periods=50, freq="5min")
    bars = pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0},
        index=index,
    )
    features = SimpleNamespace(bar_ts=current_bar)
    monkeypatch.setattr(engine, "_load_many", lambda *_args: {"TEST": bars})
    monkeypatch.setattr(engine_module, "add_indicators", lambda frame: frame)
    monkeypatch.setattr(engine_module, "compute_features", lambda _frame: features)
    monkeypatch.setattr(engine_module, "simulate", lambda *_args: ("SL", -1.0))
    monkeypatch.setattr(engine_module, "measure_excursions", lambda *_args: (1.2, 0.4, 2, current_bar.to_pydatetime()))
    monkeypatch.setattr(engine_module, "measure_target_times", lambda *_args: (1, None))
    return engine, store, now


@pytest.mark.parametrize(
    ("failed_method", "reject_key"),
    [
        ("update_excursions", "excursion_storage_error"),
        ("update_target_times", "target_time_storage_error"),
    ],
)
def test_closed_outcome_waits_for_all_training_metrics_to_persist(
    monkeypatch, failed_method, reject_key
):
    store = MetricsStore(fail_method=failed_method)
    engine, store, now = _engine_with_open_signal(monkeypatch, store)

    result = engine.cycle("normal", now)
    row = store.rows[0]

    assert result["rejects"][reject_key] == 1
    assert row["status"] == "OPEN"
    assert row["r_result"] is None
    assert store.outcome_calls == 0


def test_outcome_closes_after_training_metrics_persist(monkeypatch):
    store = MetricsStore()
    engine, store, now = _engine_with_open_signal(monkeypatch, store)

    result = engine.cycle("normal", now)
    row = store.rows[0]

    assert result["rejects"].get("excursion_storage_error", 0) == 0
    assert result["rejects"].get("target_time_storage_error", 0) == 0
    assert row["status"] == "SL"
    assert row["r_result"] == -1.0
    assert row["mfe_r"] == 1.2
    assert row["mae_r"] == 0.4
    assert row["bars_held"] == 2
    assert row["bars_to_tp1"] == 1
    assert store.outcome_calls == 1
