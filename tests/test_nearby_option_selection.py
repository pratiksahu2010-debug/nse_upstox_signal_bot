from collections import Counter
from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from app import engine as engine_module
from app.config import IST, NORMAL
from app.db import MemoryStore
from app.engine import Engine


ATM_KEY = "NSE_FO|ATM"
LIQUID_KEY = "NSE_FO|LIQUID"


class EmptyStream:
    def quote(self, _key):
        return None


class CandidateQuoteClient:
    def __init__(self, now, fail=False):
        self.now = now
        self.fail = fail
        self.calls = []

    def full_quotes(self, keys):
        self.calls.append(list(keys))
        if self.fail:
            raise RuntimeError("quote endpoint unavailable")
        return {
            ATM_KEY: {
                "last_price": 10.0, "timestamp": self.now.timestamp(),
                "volume": 500, "oi": 1000,
                "depth": {"buy": [{"price": 9.9, "quantity": 250}], "sell": [{"price": 10.1, "quantity": 250}]},
            },
            LIQUID_KEY: {
                "last_price": 12.0, "timestamp": self.now.timestamp(),
                "volume": 5000, "oi": 8000,
                "depth": {"buy": [{"price": 11.97, "quantity": 1500}], "sell": [{"price": 12.03, "quantity": 1500}]},
            },
        }


def _run(monkeypatch, *, fail=False):
    now = datetime(2026, 11, 19, 10, 5, tzinfo=IST)
    client = CandidateQuoteClient(now, fail=fail)
    store = MemoryStore()
    engine = Engine(client, store, notify=lambda _message: True)
    engine.market_stream = EmptyStream()
    engine._sync_market_stream_keys = lambda: None
    engine._deliver_pending = lambda _diag, _now: None
    engine._option_for = lambda _sig, _now: {
        "tsym": "TEST100CE", "lot": 25, "strike": 100.0, "ltp": None,
        "_key": ATM_KEY,
        "_candidates": [
            {"tsym": "TEST100CE", "lot": 25, "strike": 100.0, "_key": ATM_KEY},
            {"tsym": "TEST105CE", "lot": 25, "strike": 105.0, "_key": LIQUID_KEY},
        ],
    }

    monkeypatch.setattr(engine_module, "OPTION_MARKET_SNAPSHOT", False)
    monkeypatch.setattr(engine_module, "OPTION_LIQUIDITY_CHECKS", False)
    monkeypatch.setattr(engine_module, "OPTION_PREMIUM_TRACKING", True)
    monkeypatch.setattr(engine_module, "OPTION_QUOTE", False)
    monkeypatch.setattr(engine_module, "OPTION_MAX_QUOTE_AGE_SEC", 20)
    monkeypatch.setattr(engine_module, "OPTION_MIN_VOLUME", 100)
    monkeypatch.setattr(engine_module, "OPTION_MIN_OI", 500)
    monkeypatch.setattr(engine_module, "OPTION_MAX_SPREAD_PCT", 3.0)
    monkeypatch.setattr(engine_module, "format_signal", lambda _sig: "test signal")
    monkeypatch.setattr(
        "app.telegram_polling.get_persistent_state",
        lambda _store, _key, default=None: default,
    )

    signal = SimpleNamespace(
        score=90, symbol="RELIANCE", side="LONG",
        bar_ts=pd.Timestamp("2026-11-19 10:00:00", tz=IST),
        entry=100.0, sl=98.0, tp1=103.0, tp2=104.0,
        trigger="TEST", reasons=[], atr=1.0,
        indicator_snapshot={"adx": 25.0}, option=None,
    )
    diag = {"rejects": Counter()}
    sent = engine._dispatch("normal", NORMAL, [signal], now, False, diag)
    return sent, client, store


def test_dispatch_selects_best_fresh_nearby_contract_from_one_batch(monkeypatch):
    sent, client, store = _run(monkeypatch)

    assert sent == 1
    assert client.calls == [[ATM_KEY, LIQUID_KEY]]
    option = store.rows[0]["details"]["option"]
    assert option["tsym"] == "TEST105CE"
    assert option["selection_status"] == "LIQUIDITY_VERIFIED"
    assert option["spread_pct"] == 0.5
    assert store.rows[0]["option_tracking"]["selection_status"] == "LIQUIDITY_VERIFIED"


def test_dispatch_keeps_atm_and_labels_unverified_when_quotes_unavailable(monkeypatch):
    sent, client, store = _run(monkeypatch, fail=True)

    assert sent == 1
    # The nearby batch failed, then the selected ATM contract received its
    # own REST retry. Both failures are tolerated and the alert is explicitly
    # labelled unverified rather than falsely marked liquidity-verified.
    assert client.calls == [[ATM_KEY, LIQUID_KEY], [ATM_KEY]]
    option = store.rows[0]["details"]["option"]
    assert option["tsym"] == "TEST100CE"
    assert option["selection_status"] == "ATM_FALLBACK_UNVERIFIED"
    assert store.rows[0]["option_tracking"]["selection_status"] == "ATM_FALLBACK_UNVERIFIED"
