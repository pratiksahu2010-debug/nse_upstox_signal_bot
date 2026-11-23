from datetime import datetime, timedelta

from app.common import SymbolInfo
from app.config import IST
from app.db import MemoryStore
from app.engine import Engine
from app.universe import Universe


def _universe(now, symbols, ready):
    return Universe(
        normal=[SymbolInfo(symbol, f"NSE_EQ|{symbol}", "normal") for symbol in symbols],
        built_on=now.strftime("%Y-%m-%d"),
        configured_symbols=list(symbols),
        ready=ready,
        validation_errors=[] if ready else ["fixture incomplete"],
    )


def test_incomplete_universe_retries_after_five_minute_backoff():
    now = datetime(2026, 11, 23, 9, 0, tzinfo=IST)
    calls = []

    def builder(at):
        calls.append(at)
        if len(calls) == 1:
            return _universe(at, ["AAA"], ready=False)
        return _universe(at, ["AAA", "BBB"], ready=True)

    engine = Engine(client=object(), store=MemoryStore(), universe_builder=builder)
    engine.ensure_universe(now)
    assert engine.uni.ready is False
    assert len(calls) == 1

    engine.ensure_universe(now + timedelta(minutes=4))
    assert len(calls) == 1

    engine.ensure_universe(now + timedelta(minutes=5))
    assert len(calls) == 2
    assert engine.uni.ready is True
    assert [item.symbol for item in engine.uni.normal] == ["AAA", "BBB"]


def test_universe_builder_exception_is_backed_off_and_retried():
    now = datetime(2026, 11, 23, 9, 0, tzinfo=IST)
    calls = []

    def builder(at):
        calls.append(at)
        if len(calls) == 1:
            raise RuntimeError("temporary instrument master failure")
        return _universe(at, ["AAA"], ready=True)

    engine = Engine(client=object(), store=MemoryStore(), universe_builder=builder)
    try:
        engine.ensure_universe(now)
    except RuntimeError as exc:
        assert "temporary instrument master failure" in str(exc)
    else:
        raise AssertionError("first universe build should fail")

    engine.ensure_universe(now + timedelta(minutes=1))
    assert len(calls) == 1

    engine.ensure_universe(now + timedelta(minutes=5))
    assert len(calls) == 2
    assert engine.uni.ready is True

def test_fixed_fno_universe_replaces_exiting_underlyings_without_changing_size():
    from app.symbols import (
        EXPECTED_UNIVERSE_SIZE,
        FIXED_FNO_SYMBOLS,
        FIXED_INDEX_SYMBOLS,
        FIXED_STOCK_SYMBOLS,
    )

    assert len(FIXED_STOCK_SYMBOLS) == 144
    assert len(FIXED_INDEX_SYMBOLS) == 6
    assert len(FIXED_FNO_SYMBOLS) == EXPECTED_UNIVERSE_SIZE == 150
    assert len(set(FIXED_FNO_SYMBOLS)) == 150
    assert "IEX" not in FIXED_FNO_SYMBOLS
    assert "IREDA" not in FIXED_FNO_SYMBOLS
    assert "RELIANCE" in FIXED_FNO_SYMBOLS
    assert "SBIN" in FIXED_FNO_SYMBOLS
