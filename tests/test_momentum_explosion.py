from datetime import time
from types import SimpleNamespace

import pandas as pd

from app import mod_momentum
from app.common import SymbolInfo


def _features(**overrides):
    values = dict(
        bar_ts=pd.Timestamp("2026-11-20 10:00:00+05:30"),
        bar_end=pd.Timestamp("2026-11-20 10:05:00+05:30"),
        open=100.0, high=103.0, low=99.0, close=102.5, volume=5000.0,
        ema9=102.0, ema21=101.0, vwap=101.0, rsi=65.0,
        adx=35.0, adx_prev2=30.0, pdi=30.0, mdi=10.0, atr=1.0,
        rvol=1.5, body_frac=0.8, close_pos=0.9, range_atr=4.0,
        htf_close=102.0, htf_ema9=101.5, htf_ema21=100.5,
        htf_pdi=28.0, htf_mdi=10.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _breakout_frame():
    index = pd.date_range("2026-11-20 08:00", periods=50, freq="5min", tz="+05:30")
    frame = pd.DataFrame({
        "open": [99.0] * 50,
        "high": [100.0] * 49 + [103.0],
        "low": [98.5] * 50,
        "close": [99.5] * 49 + [102.5],
        "volume": [1000.0] * 49 + [5000.0],
    }, index=index)
    return frame


def test_momentum_explosion_identifies_breakout_path():
    frame = _breakout_frame()
    features = _features(rvol=1.0, range_atr=1.2, body_frac=0.8)

    assert mod_momentum._path(frame, features, 1) == "BREAKOUT"


def test_momentum_explosion_requires_5m_and_completed_15m_trend_alignment():
    features = _features(htf_pdi=8.0, htf_mdi=25.0)

    assert mod_momentum._trend_side(features) == 0


def test_momentum_explosion_emits_scored_signal_for_aligned_compression_breakout(monkeypatch):
    monkeypatch.setattr(mod_momentum, "C", SimpleNamespace(
        first_signal=time(9, 20), last_entry=time(15, 0), score_min=78,
        adx_min=19.0, sl_atr=1.2, tp1_r=1.5, tp2_r=2.0,
    ))
    frame = _breakout_frame()
    # The prior six bars are narrow and current candle closes above their range.
    frame.loc[frame.index[-7:-1], "high"] = 99.8
    frame.loc[frame.index[-7:-1], "low"] = 99.2
    features = _features(rvol=1.5, range_atr=1.2, body_frac=0.8)
    info = SymbolInfo("TEST", "NSE_EQ|TEST", "normal", tick=0.05)
    signal, reason = mod_momentum.evaluate(
        info, frame, features, SimpleNamespace(ignore_time=True),
    )

    assert reason is None
    assert signal is not None
    assert signal.module == "momentum"
    assert signal.trigger == "MOMENTUM_EXPLOSION_COMPRESSION"
    assert signal.score >= 78
