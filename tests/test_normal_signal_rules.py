from types import SimpleNamespace

from app.config import ENABLE_MOMENTUM
from app import mod_normal


def _features(**overrides):
    values = {
        "adx": 25.0,
        "ema9": 105.0,
        "ema21": 103.0,
        "close": 106.0,
        "vwap": 104.0,
        "atr": 2.0,
        "rsi": 55.0,
        "htf_close": 105.0,
        "htf_ema9": 104.0,
        "htf_ema21": 102.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_production_configuration_is_normal_only():
    assert ENABLE_MOMENTUM is False


def test_long_setup_uses_only_selected_indicators(monkeypatch):
    monkeypatch.setattr(mod_normal, "C", SimpleNamespace(adx_min=20.0))
    # Deliberately no DI, RVOL, or candle-shape attributes: none is a gate.
    assert mod_normal.setup_side(_features()) == 1
    assert mod_normal.setup_side(_features(adx=19.99)) == 0


def test_short_setup_uses_only_selected_indicators(monkeypatch):
    monkeypatch.setattr(mod_normal, "C", SimpleNamespace(adx_min=20.0))
    f = _features(
        ema9=100.0, ema21=102.0, close=99.0, vwap=101.0,
        htf_close=100.0, htf_ema9=101.0, htf_ema21=103.0,
    )
    assert mod_normal.setup_side(f) == -1


def test_score_uses_selected_indicator_features_only():
    f = _features()
    score = mod_normal.score_signal(f, 1)
    assert 0 <= score <= 100
