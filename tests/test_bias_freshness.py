from datetime import datetime
import pandas as pd

from app.config import IST
from app.db import MemoryStore
from app.engine import Engine
from app.universe import Universe


def test_insufficient_index_data_resets_previous_bias():
    now = datetime(2026, 11, 17, 10, 5, tzinfo=IST)
    engine = Engine(object(), MemoryStore())
    engine.bias = "bullish"
    engine.bias_at = now

    assert engine._set_bias(pd.DataFrame(), now) is False
    assert engine.bias == "neutral"
    assert engine.bias_at is None


def test_missing_index_instrument_resets_previous_bias():
    now = datetime(2026, 11, 17, 10, 5, tzinfo=IST)
    engine = Engine(object(), MemoryStore())
    engine.uni = Universe(built_on=now.strftime("%Y-%m-%d"))
    engine.bias = "bearish"
    engine.bias_at = now - pd.Timedelta(minutes=10)

    engine._refresh_bias(now)

    assert engine.bias == "neutral"
    assert engine.bias_at is None


def test_new_daily_universe_does_not_inherit_previous_bias():
    now = datetime(2026, 11, 18, 8, 0, tzinfo=IST)
    old_day = datetime(2026, 11, 17, 8, 0, tzinfo=IST)
    engine = Engine(object(), MemoryStore(), universe_builder=lambda at: Universe(
        built_on=at.strftime("%Y-%m-%d")
    ))
    engine.uni = Universe(built_on=old_day.strftime("%Y-%m-%d"))
    engine.bias = "bullish"
    engine.bias_at = old_day

    engine.ensure_universe(now)

    assert engine.bias == "neutral"
    assert engine.bias_at is None
