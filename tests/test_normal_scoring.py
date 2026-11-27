from datetime import time
from types import SimpleNamespace

import pandas as pd

from app import mod_normal
from app.common import SymbolInfo


def _strong_features(**overrides):
    values = dict(
        bar_ts=pd.Timestamp("2026-11-20 10:00:00+05:30"),
        bar_end=pd.Timestamp("2026-11-20 10:05:00+05:30"),
        open=100.0, high=106.0, low=99.0, close=105.0, volume=1000.0,
        ema9=104.0, ema21=102.0, ema21_slope=1.0, vwap=103.0,
        rsi=66.0, rsi_prev=51.0, adx=35.0, adx_prev2=32.0,
        adx_max10=36.0, pdi=32.0, mdi=10.0, atr=2.0, atr_pct=2.0,
        rvol=3.0, crosses20=0, cross_age=None, body_frac=0.85,
        close_pos=0.9, range_atr=3.5, htf_bar_ts=None, htf_close=105.0,
        htf_ema9=103.0, htf_ema21=101.0, htf_pdi=30.0, htf_mdi=10.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_score_is_bounded_and_rewards_stronger_adx_and_rsi():
    strong = mod_normal.score_signal(_strong_features(), 1)
    weaker = mod_normal.score_signal(
        _strong_features(adx=19.0, rsi=52.0), 1
    )

    assert 0 <= weaker <= 100
    assert 0 <= strong <= 100
    assert strong > weaker
    assert strong >= 72


def test_score_is_directionally_symmetric_for_mirrored_5m_inputs():
    bullish = _strong_features()
    bearish = _strong_features(
        close=95.0, ema9=96.0, ema21=98.0, ema21_slope=-1.0,
        vwap=97.0, rsi=28.0,
    )

    assert mod_normal.score_signal(bullish, 1) == mod_normal.score_signal(bearish, -1)


def test_score_uses_only_ema_slope_vwap_adx_and_directional_rsi():
    baseline = mod_normal.score_signal(_strong_features(), 1)
    changed_unscored_features = _strong_features(
        pdi=0.0, mdi=100.0, rvol=0.0, body_frac=0.0,
        close_pos=0.0, range_atr=100.0, atr_pct=99.0,
        volume=0.0, crosses20=20, cross_age=0,
        htf_close=1.0, htf_ema9=999.0, htf_ema21=1.0,
        htf_pdi=0.0, htf_mdi=100.0,
    )

    # These fields may be useful diagnostics, but they are intentionally not
    # score inputs. The Normal score remains strictly 5-minute and deterministic.
    assert mod_normal.score_signal(changed_unscored_features, 1) == baseline


def test_score_reaches_documented_floor_and_ceiling():
    minimum = _strong_features(
        ema9=100.0, ema21=100.0, ema21_slope=0.0,
        close=100.0, vwap=100.0, adx=20.0, rsi=52.0,
    )
    maximum = _strong_features(
        ema9=110.0, ema21=100.0, ema21_slope=10.0,
        close=110.0, vwap=100.0, adx=100.0, rsi=100.0,
    )

    assert mod_normal.score_signal(minimum, 1) == 60
    assert mod_normal.score_signal(maximum, 1) == 100


def test_final_score_gate_is_the_only_score_gate(monkeypatch):
    monkeypatch.setattr(mod_normal, "C", SimpleNamespace(
        first_signal=time(9, 20), last_entry=time(15, 0), score_min=97,
        adx_min=19.0, sl_atr=1.2, tp1_r=1.5, tp2_r=2.0,
    ))
    features = _strong_features()
    info = SymbolInfo("TEST", "NSE_EQ|TEST", "normal", tick=0.05)
    signal, reason = mod_normal.evaluate(
        info, None, features, SimpleNamespace(ignore_time=True)
    )

    assert signal is None
    assert reason == "score_min"


def test_signal_is_emitted_with_score_when_final_gate_is_met(monkeypatch):
    monkeypatch.setattr(mod_normal, "C", SimpleNamespace(
        first_signal=time(9, 20), last_entry=time(15, 0), score_min=72,
        adx_min=19.0, sl_atr=1.2, tp1_r=1.5, tp2_r=2.0,
    ))
    features = _strong_features()
    info = SymbolInfo("TEST", "NSE_EQ|TEST", "normal", tick=0.05)
    signal, reason = mod_normal.evaluate(
        info, None, features, SimpleNamespace(ignore_time=True)
    )

    assert reason is None
    assert signal is not None
    assert signal.score >= 72
    assert any("Final quality score" in item for item in signal.reasons)
