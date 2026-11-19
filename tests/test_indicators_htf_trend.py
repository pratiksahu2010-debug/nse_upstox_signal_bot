import numpy as np
import pandas as pd

from app import indicators
from app.config import IST


def test_htf_trend_excludes_incomplete_intervals_even_when_they_are_not_latest(monkeypatch):
    index = pd.date_range("2026-11-18 09:15", periods=90, freq="5min", tz=IST)
    close = np.linspace(100.0, 130.0, len(index))
    frame = pd.DataFrame({
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.full(len(index), 1000.0),
    }, index=index)

    # Remove one source candle from a middle 15m interval, leaving two of three.
    frame = frame.drop(index[31])
    observed_lengths = []

    def capture_length(series, period):
        observed_lengths.append(len(series))
        return pd.Series(0.0, index=series.index)

    monkeypatch.setattr(indicators, "ema", capture_length)

    assert indicators.htf_trend(frame) == "neutral"
    # There are 30 intervals total; exactly one is incomplete and must be excluded.
    assert observed_lengths == [29, 29]
