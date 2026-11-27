import numpy as np
import pandas as pd

from app.config import IST
from app.indicators import add_indicators, compute_features, rvol_time_of_day


def _frame(periods=60):
    index = pd.date_range("2026-11-18 09:15", periods=periods, freq="5min", tz=IST)
    close = np.linspace(100.0, 106.0, periods)
    return pd.DataFrame({
        "open": close - 0.1,
        "high": close + 0.3,
        "low": close - 0.3,
        "close": close,
        "volume": np.full(periods, 1000.0),
    }, index=index)


def test_features_fail_closed_for_duplicate_or_out_of_order_timestamps():
    frame = add_indicators(_frame())
    duplicate = pd.concat([frame.iloc[:-1], frame.iloc[[-2]], frame.iloc[[-1]]])
    assert compute_features(duplicate) is None
    assert compute_features(frame.iloc[::-1]) is None


def test_features_fail_closed_for_invalid_recent_ohlcv():
    frame = _frame()
    frame.iloc[-1, frame.columns.get_loc("volume")] = -1
    assert compute_features(add_indicators(frame)) is None

    frame = _frame()
    frame.iloc[-1, frame.columns.get_loc("high")] = frame.iloc[-1]["low"] - 1
    assert compute_features(add_indicators(frame)) is None


def test_relative_volume_never_returns_nan_or_infinity_for_bad_volume():
    frame = _frame()
    frame.iloc[-1, frame.columns.get_loc("volume")] = np.nan
    assert rvol_time_of_day(frame) == 0.0

    frame.iloc[-1, frame.columns.get_loc("volume")] = 1000.0
    frame.iloc[-2, frame.columns.get_loc("volume")] = np.inf
    assert np.isfinite(rvol_time_of_day(frame))
