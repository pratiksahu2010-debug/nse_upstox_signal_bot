from datetime import time
from types import SimpleNamespace

import pandas as pd
import pytest

from app import mod_normal
from app.common import SymbolInfo


def _features(side=1):
    if side > 0:
        return SimpleNamespace(
            bar_ts=pd.Timestamp("2026-11-20 10:00:00+05:30"),
            bar_end=pd.Timestamp("2026-11-20 10:05:00+05:30"),
            open=100.0, high=106.0, low=99.0, close=105.03,
            ema9=104.0, ema21=102.0, ema21_slope=1.0, vwap=103.0,
            rsi=66.0, rsi_prev=51.0, adx=35.0, atr=1.03,
        )
    return SimpleNamespace(
        bar_ts=pd.Timestamp("2026-11-20 10:00:00+05:30"),
        bar_end=pd.Timestamp("2026-11-20 10:05:00+05:30"),
        open=100.0, high=101.0, low=94.0, close=95.03,
        ema9=96.0, ema21=98.0, ema21_slope=-1.0, vwap=97.0,
        rsi=28.0, rsi_prev=43.0, adx=35.0, atr=1.03,
    )


@pytest.mark.parametrize("side", [1, -1])
def test_tick_rounding_preserves_minimum_stop_distance_and_rr(monkeypatch, side):
    monkeypatch.setattr(mod_normal, "C", SimpleNamespace(
        first_signal=time(9, 20), last_entry=time(15, 0), score_min=72,
        adx_min=20.0, sl_atr=1.2, tp1_r=1.5, tp2_r=2.0,
    ))
    signal, reason = mod_normal.evaluate(
        SymbolInfo("TEST", "NSE_EQ|TEST", "normal", tick=0.05),
        pd.DataFrame(), _features(side), SimpleNamespace(ignore_time=True),
    )

    assert reason is None
    assert signal is not None
    risk = abs(signal.entry - signal.sl)
    assert risk >= pytest.approx(1.2 * 1.03, abs=1e-9)
    assert abs(signal.tp1 - signal.entry) / risk >= 1.5
    assert abs(signal.tp2 - signal.entry) / risk >= 2.0
    assert round(signal.entry / 0.05) * 0.05 == pytest.approx(signal.entry)
    for price in (signal.sl, signal.tp1, signal.tp2):
        assert price / 0.05 == pytest.approx(round(price / 0.05))


@pytest.mark.parametrize(
    "sl_atr,tp1_r,tp2_r",
    [(0.0, 1.5, 2.0), (-1.0, 1.5, 2.0), (1.2, 0.0, 2.0),
     (1.2, 2.0, 1.5)],
)
def test_invalid_risk_configuration_fails_closed(monkeypatch, sl_atr, tp1_r, tp2_r):
    monkeypatch.setattr(mod_normal, "C", SimpleNamespace(
        first_signal=time(9, 20), last_entry=time(15, 0), score_min=72,
        adx_min=20.0, sl_atr=sl_atr, tp1_r=tp1_r, tp2_r=tp2_r,
    ))
    signal, reason = mod_normal.evaluate(
        SymbolInfo("TEST", "NSE_EQ|TEST", "normal", tick=0.05),
        pd.DataFrame(), _features(1), SimpleNamespace(ignore_time=True),
    )

    assert signal is None
    assert reason == "invalid_risk_config"
