from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import pandas as pd

from app import mod_normal
from app import engine as engine_module
from app.common import Signal, SymbolInfo
from app.engine import Engine, SymbolState, _triggered_to_cooldown, _trigger_state_after_persist
from app.db import MemoryStore


def _features(**overrides):
    values = dict(ema9=101.0, ema21=100.0, close=102.0, vwap=100.5,
                  adx=25.0, pdi=30.0, mdi=15.0, rsi_prev=51.5, rsi=52.0,
                  htf_close=102.0, htf_ema9=101.0, htf_ema21=100.0,
                  htf_pdi=30.0, htf_mdi=15.0)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_normal_setup_uses_5m_ema_vwap_and_adx_only():
    f = _features(htf="neutral")
    assert mod_normal.setup_side(f) == 1
    assert mod_normal.setup_side(_features(
        ema9=99, ema21=100, close=99, vwap=100, pdi=12, mdi=25,
        htf_close=98, htf_ema9=99, htf_ema21=100, htf_pdi=10, htf_mdi=20,
    )) == -1


def test_rsi_cross_thresholds_are_directional_and_exact():
    assert mod_normal.rsi_triggered(_features(rsi_prev=51.9, rsi=52.0), 1)
    assert not mod_normal.rsi_triggered(_features(rsi_prev=52.0, rsi=54.0), 1)
    assert mod_normal.rsi_triggered(_features(rsi_prev=42.1, rsi=42.0), -1)
    assert not mod_normal.rsi_triggered(_features(rsi_prev=42.0, rsi=40.0), -1)


def test_normal_setup_state_survives_memory_store_reload():
    store = MemoryStore()
    payload = {"phase": "COOLDOWN", "side": 1, "last_bar": "2026-11-18T10:00:00+05:30",
               "cooldown_remaining": 2, "invalidated": True}
    store.save_normal_state(date(2026, 11, 18), "TEST", payload)
    assert store.load_normal_states(date(2026, 11, 18)) == {"TEST": payload}
    assert store.load_normal_states(date(2026, 11, 19)) == {}


def test_15m_context_is_not_required_or_used():
    # Opposite and missing higher-timeframe values cannot veto a valid 5m setup.
    assert mod_normal.setup_side(_features(
        htf_close=99.0, htf_ema9=100.0, htf_ema21=101.0,
    )) == 1
    assert mod_normal.setup_side(_features(
        htf_close=None, htf_ema9=None, htf_ema21=None,
    )) == 1
    # A bearish setup is decided from the completed 5m candle only.
    assert mod_normal.setup_side(_features(
        ema9=99.0, ema21=100.0, close=99.0, vwap=100.0,
        htf_close=102.0, htf_ema9=101.0, htf_ema21=100.0,
    )) == -1



def test_triggered_lifecycle_advances_to_four_bar_cooldown_on_next_completed_candle():
    triggered = {
        "phase": "TRIGGERED", "side": 1, "trigger_bar": "2026-11-18T10:00:00+05:30",
        "last_bar": "2026-11-18T10:00:00+05:30",
        "cooldown_remaining": 4, "invalidated": False,
    }
    next_state = _triggered_to_cooldown(triggered, current_side=1)
    assert next_state["phase"] == "COOLDOWN"
    assert next_state["side"] == 1
    assert next_state["cooldown_remaining"] == 3
    assert next_state["invalidated"] is False
    # The input is unchanged so a persisted snapshot remains a faithful record.
    assert triggered["phase"] == "TRIGGERED"


def test_triggered_lifecycle_carries_setup_invalidation_into_cooldown():
    triggered = {
        "phase": "TRIGGERED", "side": -1,
        "cooldown_remaining": 4, "invalidated": False,
    }
    next_state = _triggered_to_cooldown(triggered, current_side=0)
    assert next_state["phase"] == "COOLDOWN"
    assert next_state["side"] == -1
    assert next_state["cooldown_remaining"] == 3
    assert next_state["invalidated"] is True


def test_triggered_phase_survives_memory_store_restart():
    store = MemoryStore()
    payload = {
        "phase": "TRIGGERED", "side": 1,
        "last_bar": "2026-11-18T10:00:00+05:30",
        "trigger_bar": "2026-11-18T10:00:00+05:30",
        "cooldown_remaining": 4, "invalidated": False,
    }
    store.save_normal_state(date(2026, 11, 18), "TEST", payload)
    assert store.load_normal_states(date(2026, 11, 18)) == {"TEST": payload}


def test_unpersisted_trigger_does_not_consume_cooldown():
    triggered = {
        "phase": "TRIGGERED", "side": 1,
        "last_bar": "2026-11-19T10:00:00+05:30",
        "trigger_bar": "2026-11-19T10:00:00+05:30",
        "cooldown_remaining": 4, "invalidated": False,
    }

    result = _trigger_state_after_persist(triggered, persisted=False)

    assert result["phase"] == "IDLE"
    assert result["side"] == 0
    assert result["last_bar"] == triggered["last_bar"]
    assert result["cooldown_remaining"] == 0
    assert "trigger_bar" not in result
    assert triggered["phase"] == "TRIGGERED"


def test_persisted_trigger_enters_existing_four_bar_cooldown_stage():
    triggered = {
        "phase": "TRIGGERED", "side": -1,
        "last_bar": "2026-11-19T10:00:00+05:30",
        "trigger_bar": "2026-11-19T10:00:00+05:30",
        "cooldown_remaining": 4, "invalidated": False,
    }

    result = _trigger_state_after_persist(triggered, persisted=True)

    assert result == triggered


@pytest.mark.parametrize("initial_phase", ["IDLE", "ARMED"])
def test_cycle_commits_triggered_only_when_dispatch_persisted_signal(monkeypatch, initial_phase):
    now = datetime(2026, 11, 19, 10, 5, 10, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    bar_ts = pd.Timestamp("2026-11-19T10:00:00+05:30")
    info = SymbolInfo("TEST", "NSE_EQ|TEST", "normal", underlying="TEST")
    store = MemoryStore()
    engine = Engine(client=object(), store=store, notify=lambda _message: True)
    state = {
        "phase": initial_phase, "side": 1,
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
    monkeypatch.setattr(mod_normal, "setup_side", lambda _features: 1)
    monkeypatch.setattr(mod_normal, "rsi_triggered", lambda _features, _side: True)
    monkeypatch.setattr(mod_normal, "evaluate", lambda *_args: (signal, None))
    monkeypatch.setattr(engine_module, "ENABLE_MOMENTUM", False)

    persisted = [False]

    def dispatch(module, _cfg, candidates, _now, _force, diag):
        if module == "momentum":
            assert candidates == []
            return 0
        if not candidates:
            return 0
        assert candidates == [signal]
        if persisted[0]:
            diag["_persisted_candidates"].add(("TEST", bar_ts.isoformat()))
        return 1 if persisted[0] else 0

    monkeypatch.setattr(engine, "_dispatch", dispatch)

    engine.cycle("normal", now)
    assert store.load_normal_states(now.date())["TEST"]["phase"] == "IDLE"

    # Reset to the original lifecycle phase and simulate a durable signal insert.
    armed_again = dict(state)
    store.save_normal_state(now.date(), "TEST", armed_again)
    engine.states[("normal", "TEST")].normal_state = armed_again
    persisted[0] = True
    engine.cycle("normal", now)
    persisted_state = store.load_normal_states(now.date())["TEST"]
    assert persisted_state["phase"] == "TRIGGERED"
    assert persisted_state["trigger_bar"] == bar_ts.isoformat()
    assert persisted_state["last_bar"] == bar_ts.isoformat()

    # The next completed candle must advance to COOLDOWN, not reset on a
    # false 10-minute gap caused by a stale persisted last_bar.
    next_bar = bar_ts + pd.Timedelta(minutes=5)
    next_features = SimpleNamespace(
        bar_ts=next_bar,
        bar_end=next_bar + pd.Timedelta(minutes=5),
        close=100.0,
    )
    monkeypatch.setattr(engine_module, "compute_features", lambda _frame: next_features)
    engine.cycle("normal", now + timedelta(minutes=5))
    cooldown_state = store.load_normal_states(now.date())["TEST"]
    assert cooldown_state["phase"] == "COOLDOWN"
    assert cooldown_state["cooldown_remaining"] == 3
    assert cooldown_state["last_bar"] == next_bar.isoformat()
