from datetime import time
from types import SimpleNamespace

from app import mod_normal
from app.common import SymbolInfo


def _strong_features(**overrides):
    values = dict(
        bar_ts=None, bar_end=None, open=100.0, high=106.0, low=99.0,
        close=105.0, volume=1000.0, ema9=104.0, ema21=102.0,
        ema21_slope=1.0, vwap=103.0, rsi=66.0, rsi_prev=51.0,
        adx=35.0, adx_prev2=32.0, adx_max10=36.0, pdi=32.0, mdi=10.0,
        atr=2.0, atr_pct=2.0, rvol=3.0, crosses20=0, cross_age=None,
        body_frac=0.85, close_pos=0.9, range_atr=3.5,
        htf_bar_ts=None, htf_close=105.0, htf_ema9=103.0,
        htf_ema21=101.0, htf_pdi=30.0, htf_mdi=10.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_score_is_bounded_and_rewards_aligned_trend_and_participation():
    strong = mod_normal.score_signal(_strong_features(), 1)
    weak = mod_normal.score_signal(_strong_features(
        adx=19.0, pdi=20.0, mdi=18.0, rsi=52.0, rvol=1.0,
        body_frac=0.2, close_pos=0.55,
    ), 1)

    assert 0 <= weak <= 100
    assert 0 <= strong <= 100
    assert strong > weak
    assert strong >= 72


def test_final_score_gate_is_the_only_score_gate(monkeypatch):
    monkeypatch.setattr(mod_normal, "C", SimpleNamespace(
        first_signal=time(9, 20), last_entry=time(15, 0), score_min=97,
        adx_min=19.0, sl_atr=1.2, tp1_r=1.5, tp2_r=2.0,
    ))
    features = _strong_features()
    info = SymbolInfo("TEST", "NSE_EQ|TEST", "normal", tick=0.05)
    signal, reason = mod_normal.evaluate(info, None, features, SimpleNamespace(ignore_time=True))

    assert signal is None
    assert reason == "score_min"


def test_signal_is_emitted_with_score_when_final_gate_is_met(monkeypatch):
    monkeypatch.setattr(mod_normal, "C", SimpleNamespace(
        first_signal=time(9, 20), last_entry=time(15, 0), score_min=72,
        adx_min=19.0, sl_atr=1.2, tp1_r=1.5, tp2_r=2.0,
    ))
    features = _strong_features()
    info = SymbolInfo("TEST", "NSE_EQ|TEST", "normal", tick=0.05)
    signal, reason = mod_normal.evaluate(info, None, features, SimpleNamespace(ignore_time=True))

    assert reason is None
    assert signal is not None
    assert signal.score >= 72
    assert any("Final quality score" in item for item in signal.reasons)
