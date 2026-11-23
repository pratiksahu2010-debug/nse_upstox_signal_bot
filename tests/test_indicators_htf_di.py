import numpy as np
import pandas as pd

from app.config import IST
from app.indicators import add_indicators, compute_features


def _frame(index):
    close = np.linspace(100.0, 112.0, len(index))
    return pd.DataFrame({
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.full(len(index), 1000.0),
    }, index=index)


def test_features_include_completed_15m_context():
    index = pd.date_range("2026-11-18 09:15", periods=60, freq="5min", tz=IST)
    features = compute_features(add_indicators(_frame(index)))

    assert features is not None
    assert features.bar_ts == index[-1]
    assert features.ema9 > 0
    assert features.ema21 > 0
    assert features.vwap > 0
    assert features.adx >= 0
    assert features.htf_bar_ts is not None
    assert features.htf_close > 0
    assert features.htf_ema9 > 0
    assert features.htf_ema21 > 0
    assert features.htf_pdi >= 0
    assert features.htf_mdi >= 0


def test_15m_context_ignores_incomplete_current_15m_bucket():
    index = pd.date_range("2026-11-18 09:15", periods=60, freq="5min", tz=IST)
    original = compute_features(add_indicators(_frame(index)))
    extended_index = index.append(pd.DatetimeIndex([index[-1] + pd.Timedelta(minutes=5)]))
    extended = compute_features(add_indicators(_frame(extended_index)))

    assert original is not None and extended is not None
    # The added 5m candle starts a new 15m bucket; it cannot become HTF context yet.
    assert extended.htf_bar_ts == original.htf_bar_ts
