from datetime import datetime, timedelta

from app.common import SymbolInfo
from app.config import IST
from app.engine import Engine
from app.universe import Universe


def test_mcX_master_failure_is_retried_without_rebuilding_nse(monkeypatch):
    now = datetime(2026, 11, 17, 8, 40, tzinfo=IST)
    normal = SymbolInfo("TEST", "NSE_EQ|TEST", "normal", lot=1)
    mcx = SymbolInfo("GOLD", "MCX_FO|GOLD", "mcx", lot=1)
    universe = Universe(normal=[normal], built_on=now.strftime("%Y-%m-%d"),
                        mcx_error="temporary MCX master outage")
    attempts = []

    def load_mcx(at):
        attempts.append(at)
        return [mcx]

    monkeypatch.setattr("app.engine.load_mcx", load_mcx)
    engine = Engine(object(), object(), universe_builder=lambda at: universe)

    engine.ensure_universe(now)
    normal_state = engine.states[("normal", "TEST")]
    assert ("mcx", "GOLD") not in engine.states

    # A same-day retry recovers MCX and preserves already-built NSE state.
    engine.ensure_universe(now + timedelta(minutes=5))
    assert len(attempts) == 1
    assert engine.uni is universe
    assert engine.uni.mcx_error == ""
    assert engine.states[("normal", "TEST")] is normal_state
    assert engine.states[("mcx", "GOLD")].info is mcx


def test_mcX_retry_is_throttled_after_another_transient_failure(monkeypatch):
    now = datetime(2026, 11, 17, 8, 40, tzinfo=IST)
    universe = Universe(built_on=now.strftime("%Y-%m-%d"), mcx_error="temporary outage")
    attempts = []

    def load_mcx(at):
        attempts.append(at)
        raise RuntimeError("still unavailable")

    monkeypatch.setattr("app.engine.load_mcx", load_mcx)
    engine = Engine(object(), object(), universe_builder=lambda at: universe)
    engine.ensure_universe(now)
    engine.ensure_universe(now + timedelta(minutes=5))
    engine.ensure_universe(now + timedelta(minutes=9))
    assert len(attempts) == 1
    engine.ensure_universe(now + timedelta(minutes=10))
    assert len(attempts) == 2
