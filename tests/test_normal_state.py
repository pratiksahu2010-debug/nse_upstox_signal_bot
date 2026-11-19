from types import SimpleNamespace

from app import mod_normal
from app.db import MemoryStore
from datetime import date


def _features(**overrides):
    values = dict(ema9=101.0, ema21=100.0, close=102.0, vwap=100.5,
                  adx=25.0, pdi=30.0, mdi=15.0, rsi_prev=51.5, rsi=52.0)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_normal_setup_uses_5m_gates_without_15m_confirmation():
    f = _features(htf="neutral")
    assert mod_normal.setup_side(f) == 1
    assert mod_normal.setup_side(_features(ema9=99, ema21=100, close=99, vwap=100,
                                           pdi=12, mdi=25, htf="neutral")) == -1


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


def test_15m_di_is_informational_and_never_changes_5m_setup_eligibility():
    # A bullish 5m CALL remains valid with bearish 15m DI.
    assert mod_normal.setup_side(_features(htf_pdi=10.0, htf_mdi=35.0)) == 1
    # A bearish 5m PUT remains valid with bullish 15m DI.
    assert mod_normal.setup_side(_features(
        ema9=99.0, ema21=100.0, close=99.0, vwap=100.0,
        pdi=12.0, mdi=25.0, htf_pdi=40.0, htf_mdi=8.0,
    )) == -1
    # Missing 15m DI also cannot block either 5m setup.
    assert mod_normal.setup_side(_features(htf_pdi=None, htf_mdi=None)) == 1
