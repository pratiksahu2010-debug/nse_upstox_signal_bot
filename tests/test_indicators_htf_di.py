import numpy as np
import pandas as pd

from app.config import IST
from app.indicators import htf_di_context


def _trend_frame(direction=1, bars=45):
    index = pd.date_range("2026-11-18 09:15", periods=bars, freq="5min", tz=IST)
    base = 100.0 + direction * np.arange(bars) * 0.5
    if direction > 0:
        open_ = base
        high = base + 1.0
        low = base - 0.2
        close = base + 0.8
    else:
        open_ = base
        high = base + 0.2
        low = base - 1.0
        close = base - 0.8
    return pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close,
        "volume": np.full(bars, 1000.0),
    }, index=index)


def test_htf_di_context_identifies_bullish_and_bearish_completed_15m_trends():
    bull = htf_di_context(_trend_frame(1))
    bear = htf_di_context(_trend_frame(-1))

    assert bull[0] is not None and bull[0] > bull[1]
    assert bear[1] is not None and bear[1] > bear[0]


def test_htf_di_context_ignores_incomplete_15m_bin():
    complete = _trend_frame(1)
    partial = pd.concat([complete, _trend_frame(1, bars=46).tail(1)])
    first = htf_di_context(complete)
    second = htf_di_context(partial)

    assert first == second


def test_htf_di_context_returns_unavailable_during_warmup():
    assert htf_di_context(_trend_frame(1, bars=30)) == (None, None, None)
