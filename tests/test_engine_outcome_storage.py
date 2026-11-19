import pandas as pd
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import engine as engine_module, mod_normal
from app.common import Signal, SymbolInfo
from app.db import MemoryStore
from app.engine import Engine, SymbolState


class FailingOutcomeStore(MemoryStore):
    def update_outcome(self, sid, status, r_result):
        raise RuntimeError("temporary outcome write failure")


def test_outcome_write_failure_does_not_abort_new_signal_scan(monkeypatch):
    now = datetime(2026, 11, 19, 10, 5, 10, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    bar_ts = pd.Timestamp("2026-11-19T10:00:00+05:30")
    info = SymbolInfo("TEST", "NSE_EQ|TEST", "normal", underlying="TEST")
    store = FailingOutcomeStore()
    prior = {
        "module": "normal", "symbol": "TEST", "side": "LONG",
        "bar_ts": (bar_ts - pd.Timedelta(minutes=10)).to_pydatetime(),
        "trade_day": now.date(), "entry": 100.0, "sl": 98.0,
        "tp1": 103.0, "tp2": 106.0, "score": 80, "trig": "PRIOR",
        "details": {"telegram_text": "prior"},
    }
    assert store.add_signal(prior) is not None

    engine = Engine(client=object(), store=store, notify=lambda _message: True)
    state = {
        "phase": "ARMED", "side": 1,
        "last_bar": (bar_ts - pd.Timedelta(minutes=5)).isoformat(),
        "cooldown_remaining": 0, "invalidated": False,
    }
    engine.states = {("normal", "TEST"): SymbolState(info, normal_state=state)}
    engine.uni = SimpleNamespace(ready=True)
    bars = pd.DataFrame({"close": [100.0] * 50})
    features = SimpleNamespace(bar_ts=bar_ts, bar_end=bar_ts + pd.Timedelta(minutes=5), close=100.0)
    signal = Signal(
        module="normal", symbol="TEST", side="LONG", bar_ts=bar_ts,
        entry=100.0, sl=98.0, tp1=103.0, tp2=106.0, atr=1.0,
        score=90, trigger="TEST", info=info,
    )

    monkeypatch.setattr(engine, "_load_many", lambda *_args: {"TEST": bars})
    monkeypatch.setattr(engine_module, "add_indicators", lambda frame: frame)
    monkeypatch.setattr(engine_module, "compute_features", lambda _frame: features)
    monkeypatch.setattr(engine_module, "simulate", lambda *_args: ("SL", -1.0))
    monkeypatch.setattr(engine_module, "measure_excursions", lambda *_args: (0.0, 0.0, 0, None))
    monkeypatch.setattr(engine_module, "measure_target_times", lambda *_args: (None, None))
    monkeypatch.setattr(engine_module, "OPTION_PREMIUM_TRACKING", False)
    monkeypatch.setattr(mod_normal, "setup_side", lambda _features: 1)
    monkeypatch.setattr(mod_normal, "rsi_triggered", lambda _features, _side: True)
    monkeypatch.setattr(mod_normal, "evaluate", lambda *_args: (signal, None))

    def dispatch(_module, _cfg, candidates, _now, _force, diag):
        assert candidates == [signal]
        # Match _dispatch: lifecycle advances only after durable persistence.
        diag["_persisted_candidates"].add((signal.symbol, signal.bar_ts.isoformat()))
        return 1

    monkeypatch.setattr(engine, "_dispatch", dispatch)

    result = engine.cycle("normal", now)

    assert result["signals"] == 1
    assert result["rejects"]["outcome_storage_error"] == 1
    assert engine.states[("normal", "TEST")].normal_state["phase"] == "TRIGGERED"
