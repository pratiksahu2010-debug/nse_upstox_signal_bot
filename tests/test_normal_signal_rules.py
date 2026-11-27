from types import SimpleNamespace

from app import mod_normal


def _features(**overrides):
    values = {
        "adx": 25.0,
        "ema9": 105.0,
        "ema21": 103.0,
        "ema21_slope": 0.5,
        "close": 106.0,
        "vwap": 104.0,
        "atr": 2.0,
        "rsi": 55.0,
        "rsi_prev": 51.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_long_setup_uses_only_completed_5m_indicators(monkeypatch):
    monkeypatch.setattr(mod_normal, "C", SimpleNamespace(adx_min=20.0))
    # No 15m, DI, RVOL, or candle-shape fields are supplied.
    assert mod_normal.setup_side(_features()) == 1
    assert mod_normal.setup_side(_features(adx=19.99)) == 0


def test_short_setup_uses_only_completed_5m_indicators(monkeypatch):
    monkeypatch.setattr(mod_normal, "C", SimpleNamespace(adx_min=20.0))
    f = _features(ema9=100.0, ema21=102.0, ema21_slope=-0.5,
                  close=99.0, vwap=101.0)
    assert mod_normal.setup_side(f) == -1


def test_5m_score_is_deterministic_from_normal_features():
    f = _features()
    score = mod_normal.score_signal(f, 1)
    assert 0 <= score <= 100
    assert score == mod_normal.score_signal(_features(), 1)
