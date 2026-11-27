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


def test_live_features_use_only_completed_5m_indicators():
    index = pd.date_range("2026-11-18 09:15", periods=150, freq="5min", tz=IST)
    features = compute_features(add_indicators(_frame(index)))

    assert features is not None
    assert features.bar_ts == index[-1]
    assert features.bar_end == index[-1] + pd.Timedelta(minutes=5)
    assert features.ema9 > 0
    assert features.ema21 > 0
    assert features.vwap > 0
    assert 0 <= features.rsi <= 100
    assert 0 <= features.adx <= 100
    assert features.atr > 0
    assert features.pdi >= 0
    assert features.mdi >= 0
