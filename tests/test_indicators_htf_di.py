import numpy as np
import pandas as pd

from app.config import IST
from app.indicators import add_indicators, compute_features


def test_features_use_only_existing_5m_indicators_without_15m_context():
    index = pd.date_range("2026-11-18 09:15", periods=60, freq="5min", tz=IST)
    close = np.linspace(100.0, 112.0, len(index))
    frame = pd.DataFrame({
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.full(len(index), 1000.0),
    }, index=index)

    features = compute_features(add_indicators(frame))

    assert features is not None
    assert features.bar_ts == index[-1]
    assert features.ema9 > 0
    assert features.ema21 > 0
    assert features.vwap > 0
    assert features.adx >= 0
    assert not any(name.startswith("htf") for name in vars(features))
