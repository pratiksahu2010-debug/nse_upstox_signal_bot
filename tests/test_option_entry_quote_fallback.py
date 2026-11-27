from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from app import engine as engine_module
from app.config import IST, NORMAL
from app.db import MemoryStore
from app.engine import Engine


OPTION_KEY = "NSE_FO|TEST_OPTION"


class EmptyStream:
    def quote(self, instrument_key):
        return None


class QuoteClient:
    def __init__(self, now, fail=False):
        self.now = now
        self.fail = fail
        self.calls = []

    def full_quotes(self, keys):
        self.calls.append(list(keys))
        if self.fail:
            raise RuntimeError("optional quote endpoint unavailable")
        return {
            OPTION_KEY: {
                "last_price": 10.0,
                "timestamp": self.now.timestamp(),
                "depth": {
                    "buy": [{"price": 9.9}],
                    "sell": [{"price": 10.1}],
                },
            }
        }


def _dispatch_with_empty_stream(monkeypatch, *, quote_failure=False, liquidity_checks=False):
    now = datetime(2026, 11, 19, 10, 5, tzinfo=IST)
    client = QuoteClient(now, fail=quote_failure)
    store = MemoryStore()
    engine = Engine(client, store, notify=lambda _message: True)
    engine.market_stream = EmptyStream()
    engine._sync_market_stream_keys = lambda: None
    engine._deliver_pending = lambda _diag, _now: None
    engine._option_for = lambda _sig, _now: {
        "_key": OPTION_KEY, "tsym": "TEST26NOV100CE", "lot": 25,
        "strike": 100.0, "ltp": None,
    }

    monkeypatch.setattr(engine_module, "OPTION_MARKET_SNAPSHOT", False)
    monkeypatch.setattr(engine_module, "OPTION_LIQUIDITY_CHECKS", liquidity_checks)
    monkeypatch.setattr(engine_module, "OPTION_PREMIUM_TRACKING", True)
    monkeypatch.setattr(engine_module, "OPTION_QUOTE", False)
    monkeypatch.setattr(engine_module, "OPTION_MAX_QUOTE_AGE_SEC", 20)
    monkeypatch.setattr(engine_module, "format_signal", lambda _sig: "test signal")
    monkeypatch.setattr(
        "app.telegram_polling.get_persistent_state",
        lambda _store, _key, default=None: default,
    )

    signal = SimpleNamespace(
        score=90,
        symbol="RELIANCE",
        side="LONG",
        bar_ts=pd.Timestamp("2026-11-19 10:00:00", tz=IST),
        entry=100.0,
        sl=98.0,
        tp1=103.0,
        tp2=104.0,
        trigger="TEST",
        reasons=[],
        atr=1.0,
        indicator_snapshot={"adx": 25.0},
        option=None,
    )
    diag = {"rejects": Counter()}
    sent = engine._dispatch(
        "normal", NORMAL, [signal], now, False, diag,
    )
    return sent, client, store, diag


def test_premium_tracking_uses_rest_fallback_when_new_stream_subscription_has_no_tick(monkeypatch):
    sent, client, store, diag = _dispatch_with_empty_stream(monkeypatch)

    assert sent == 1
    assert client.calls == [[OPTION_KEY]]
    assert len(store.rows) == 1
    tracking = store.rows[0]["option_tracking"]
    assert tracking["entry_price"] == 10.1
    assert tracking["entry_source"] == "ASK"
    assert tracking["tracking_status"] == "TRACKING"
    assert diag["rejects"].get("option_quote_missing", 0) == 0


def test_optional_rest_quote_failure_never_blocks_underlying_signal(monkeypatch):
    sent, client, store, diag = _dispatch_with_empty_stream(
        monkeypatch, quote_failure=True,
    )

    assert sent == 1
    assert client.calls == [[OPTION_KEY]]
    assert len(store.rows) == 1
    assert store.rows[0]["option_tracking"]["entry_price"] is None
    assert store.rows[0]["option_tracking"]["tracking_status"] == "MISSING_ENTRY_QUOTE"
    assert diag["rejects"].get("option_quote_missing", 0) == 0

    
def test_missing_option_quote_suppresses_signal_when_liquidity_gate_is_enabled(monkeypatch):
    sent, client, store, diag = _dispatch_with_empty_stream(
        monkeypatch, quote_failure=True, liquidity_checks=True,
    )

    assert sent == 0
    assert client.calls == [[OPTION_KEY]]
    assert len(store.rows) == 0
    assert diag["rejects"].get("option_quote_missing", 0) == 1


class FreshStream:
    def __init__(self, now):
        self.now = now

    def quote(self, instrument_key):
        return {
            "ltp": 10.0,
            "volume": 1000,
            "oi": 10000,
            "exchange_ts_ms": int((self.now.timestamp() - 2) * 1000),
            "bid": 9.9,
            "ask": 10.1,
            "bid_qty": 500,
            "ask_qty": 500,
        }


class StaleQuoteClient(QuoteClient):
    def full_quotes(self, keys):
        self.calls.append(list(keys))
        return {
            OPTION_KEY: {
                "last_price": 10.0,
                "timestamp": self.now.timestamp() - 60,
                "volume": 1000,
                "oi": 10000,
                "depth": {
                    "buy": [{"price": 9.9, "quantity": 500}],
                    "sell": [{"price": 10.1, "quantity": 500}],
                },
            }
        }


def test_fresh_stream_quote_can_validate_atm_fallback_before_liquidity_rejection(monkeypatch):
    now = datetime(2026, 11, 19, 10, 5, tzinfo=IST)
    client = StaleQuoteClient(now)
    store = MemoryStore()
    engine = Engine(client, store, notify=lambda _message: True)
    engine.market_stream = FreshStream(now)
    engine._stream_keys = []
    engine._sync_market_stream_keys = lambda: None
    engine._deliver_pending = lambda _diag, _now: None
    engine._option_for = lambda _sig, _now: {
        "_key": OPTION_KEY,
        "tsym": "TEST26NOV100CE",
        "lot": 25,
        "strike": 100.0,
        "ltp": None,
        "_candidates": [{
            "_key": OPTION_KEY,
            "tsym": "TEST26NOV100CE",
            "lot": 25,
            "strike": 100.0,
            "ltp": None,
        }],
    }

    monkeypatch.setattr(engine_module, "OPTION_MARKET_SNAPSHOT", False)
    monkeypatch.setattr(engine_module, "OPTION_LIQUIDITY_CHECKS", True)
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
        score=90,
        symbol="RELIANCE",
        side="LONG",
        bar_ts=pd.Timestamp("2026-11-19 10:00:00", tz=IST),
        entry=100.0,
        sl=98.0,
        tp1=103.0,
        tp2=104.0,
        trigger="TEST",
        reasons=[],
        atr=1.0,
        indicator_snapshot={"adx": 25.0},
        option=None,
    )
    diag = {"rejects": Counter()}
    sent = engine._dispatch("normal", NORMAL, [signal], now, False, diag)

    assert sent == 1
    assert client.calls == [[OPTION_KEY]]
    assert len(store.rows) == 1
    assert store.rows[0]["details"]["option"]["selection_status"] == "LIQUIDITY_VERIFIED"
    assert store.rows[0]["details"]["option"]["quote_age_sec"] <= 20
    assert diag["rejects"].get("option_no_liquid_contract", 0) == 0
    assert diag["rejects"].get("option_quote_stale", 0) == 0
