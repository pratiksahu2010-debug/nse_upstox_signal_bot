import gzip, io, json, os, sys, warnings
from datetime import datetime, time, timedelta
from collections import Counter

os.environ["AUTOSTART"] = "0"
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.common import Ctx, Signal, SymbolInfo
from app.config import IST, NORMAL
from app.db import MemoryStore, make_store
from app.engine import Engine
from app.indicators import add_indicators, compute_features, rvol_time_of_day
from app.tracker import simulate
from app.universe import Universe, OptionRec, resolve_nse
from app.upstox import iter_json_array
from app import mod_normal
from tests.synth import make_history


def test_rsi_flat_and_monotonic():
    idx = pd.date_range("2026-11-16 09:15", periods=60, freq="5min", tz=IST)
    flat = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 10.0}, index=idx)
    assert add_indicators(flat)["rsi"].iloc[-1] == 50.0
    up = flat.copy(); up["close"] = np.arange(60) + 100.0; up["high"] = up["close"] + .5; up["low"] = up["close"] - .5
    assert add_indicators(up)["rsi"].iloc[-1] == 100.0


def test_vwap_resets_each_session_and_adx_bounds():
    df = make_history(4, seed=3)
    ind = add_indicators(df)
    first = ind.groupby("day").head(1)
    assert np.allclose(first["vwap"], (first["high"] + first["low"] + first["close"]) / 3)
    assert ind["adx"].between(0, 100).all() and (ind["atr"] > 0).all()


def test_json_stream_chunk_boundaries():
    rows = [{"segment": "NSE_EQ", "trading_symbol": f"S{i}", "n": [1, 2, {"x": "é"}]} for i in range(500)]
    raw = gzip.compress(json.dumps(rows, indent=1).encode())
    got = list(iter_json_array(gzip.GzipFile(fileobj=io.BytesIO(raw)), chunk=97))
    assert got == rows


def _row(side="LONG"):
    return {"side": side, "entry": 100.0, "sl": 99.0, "tp1": 101.2, "tp2": 102.2,
            "bar_ts": pd.Timestamp("2026-11-17 10:00", tz=IST)}


def _bars(rows):
    idx = pd.date_range("2026-11-17 10:05", periods=len(rows), freq="5min", tz=IST)
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


def test_tracker_outcomes():
    end = time(15, 15)
    assert simulate(_row(), _bars([(100, 100.5, 98.9, 99.2)]), end)[0] == "SL"
    # SL and TP1 in one bar -> SL (conservative)
    assert simulate(_row(), _bars([(100, 101.5, 98.9, 100)]), end) == ("SL", -1.0)
    st, r = simulate(_row(), _bars([(100, 101.3, 100.1, 101), (101, 102.3, 100.9, 102)]), end)
    assert st == "TP2" and r == pytest.approx(0.5 * 1.2 + 0.5 * 2.2)
    st, r = simulate(_row(), _bars([(100, 101.3, 100.1, 101), (101, 101.4, 99.9, 100)]), end)
    assert st == "TP1_BE" and r == pytest.approx(0.6)
    short_bar = _bars([(99.5, 99.8, 98.7, 99.0)])
    assert simulate(_row("SHORT") | {"sl": 101.0, "tp1": 98.8, "tp2": 97.8},
                    short_bar, end)[0] == "TP1"


def test_universe_resolution_and_backfill():
    today = datetime(2026, 11, 17, tzinfo=IST)
    exp = int((today + timedelta(days=10)).timestamp() * 1000)
    rows = []
    for n in ("AAA", "BBB", "RES"):
        rows.append({"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": n, "instrument_key": f"NSE_EQ|{n}"})
        rows.append({"segment": "NSE_FO", "instrument_type": "FUT", "underlying_symbol": n, "expiry": exp,
                     "instrument_key": f"NSE_FO|F{n}", "lot_size": 10, "trading_symbol": f"{n} FUT"})
    rows.append({"segment": "NSE_FO", "instrument_type": "FUT", "underlying_symbol": "NIFTY", "expiry": exp,
                 "instrument_key": "NSE_FO|FNIFTY", "lot_size": 75, "trading_symbol": "NIFTY FUT"})
    rows.append({"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "GONE", "instrument_key": "NSE_EQ|GONE"})  # no F&O
    u = resolve_nse(rows, today, ["AAA", "GONE", "BBB"], ["RES"], [], [], ["NIFTY", "FINNIFTY"])
    names = [i.symbol for i in u.normal]
    assert names[0] == "NIFTY" and u.normal[0].is_index and "GONE" not in names
    assert "RES" in names and u.dropped["normal"] == ["GONE"]
    assert u.dropped["indices"] == ["FINNIFTY"]


class FakeClient:
    def __init__(self, full: pd.DataFrame, now):
        self.full, self.now, self.auth_failed = full, now, False
        self.calls = 0

    def historical(self, key, f, t):
        self.calls += 1
        return self.full[self.full.index.date < self.now.date()].assign(oi=0)

    def intraday(self, key, interval=5):
        self.calls += 1
        d = self.full[self.full.index.date == self.now.date()]
        return d[d.index <= self.now].assign(oi=0)

    def ltp(self, keys):
        return {}


def _test_universe(info, now):
    expiry = now + timedelta(days=10)
    underlying = info.underlying or info.symbol
    options = [
        OptionRec(expiry, 1000.0, "CE", "NSE_FO|TESTCE", f"{underlying} CE", 100),
        OptionRec(expiry, 1000.0, "PE", "NSE_FO|TESTPE", f"{underlying} PE", 100),
    ]
    return Universe(normal=[info], options={underlying: options},
                    built_on=now.strftime("%Y-%m-%d"))


def _find_signal_case():
    info = SymbolInfo("TEST", "K", "normal", lot=100)
    for seed in range(1, 80):
        df = make_history(7, seed=seed, last_mode="trend_down")
        ind = add_indicators(df)
        for t in [t for t in df.index if t.date() == df.index[-1].date()][5:]:
            sub = ind.loc[:t]
            f = compute_features(sub)
            if f and mod_normal.evaluate(info, sub, f, Ctx())[0]:
                return df, t
    raise AssertionError("no synthetic signal found")


def _always_normal_signal(info, df, f, ctx):
    entry = f.close
    sig = Signal(
        "normal", info.symbol, "LONG", f.bar_ts, entry, entry - 1.0,
        entry + 1.5, entry + 2.0, 1.0, 0, "ema_vwap", ["test confirmation"], info,
    )
    return sig, None


def test_engine_end_to_end_dedupe_and_cooldown(monkeypatch):
    df, t = _find_signal_case()
    monkeypatch.setattr(mod_normal, "evaluate", _always_normal_signal)
    monkeypatch.setattr(mod_normal, "setup_side", lambda f: 1)
    monkeypatch.setattr(mod_normal, "rsi_triggered", lambda f, side: f.bar_ts > pd.Timestamp(t).tz_convert(IST) + pd.Timedelta(minutes=5))
    now = t + timedelta(minutes=5, seconds=7)
    info = SymbolInfo("TEST", "K", "normal", lot=100, underlying="TEST")
    uni = _test_universe(info, now)
    sent, store = [], MemoryStore()
    eng = Engine(FakeClient(df, now), store, notify=lambda text, silent=False: sent.append(text),
                 universe_builder=lambda n: uni)
    eng.ensure_universe(now)
    baseline = eng.cycle("normal", now)
    assert baseline["signals"] == 0 and sent == []
    candle1_now = now + timedelta(minutes=5)
    eng.client.now = candle1_now
    armed = eng.cycle("normal", candle1_now)
    assert armed["signals"] == 0 and armed["watching"] == 1 and sent == []
    candle2_now = now + timedelta(minutes=10)
    eng.client.now = candle2_now
    confirmed = eng.cycle("normal", candle2_now)
    assert confirmed["signals"] == 1 and len(sent) == 1
    assert "TEST" in sent[0] and "<b>" in sent[0]
    duplicate = eng.cycle("normal", candle2_now)
    assert duplicate["signals"] == 0 and len(sent) == 1
    late = candle2_now + timedelta(minutes=15)
    eng.client.now = candle2_now
    out3 = eng.cycle("normal", late)
    assert out3["signals"] == 0

def test_force_run_still_rejects_stale_market_data(monkeypatch):
    df, t = _find_signal_case()
    original_now = t + timedelta(minutes=5, seconds=7)
    late_now = original_now + timedelta(minutes=15)
    info = SymbolInfo("TEST", "K", "normal", lot=100, underlying="TEST")
    uni = _test_universe(info, late_now)
    sent = []
    eng = Engine(FakeClient(df, original_now), MemoryStore(),
                 notify=lambda message: sent.append(message),
                 universe_builder=lambda n: uni)
    eng.ensure_universe(late_now)
    eng.states[("normal", "TEST")].normal_state = {
        "phase": "IDLE", "side": 0,
        "last_bar": pd.Timestamp(t - timedelta(minutes=5)).tz_convert(IST).isoformat(),
        "cooldown_remaining": 0, "invalidated": False,
    }
    monkeypatch.setattr("app.engine._time.sleep", lambda seconds: None)

    out = eng.cycle("normal", late_now, force=True)

    assert out["signals"] == 0
    assert out["rejects"].get("stale_data", 0) == 1
    assert sent == []


def test_load_enforces_fetch_delay_even_for_direct_or_forced_scans():
    from app.engine import SymbolState

    now = datetime(2026, 11, 17, 10, 5, 0, tzinfo=IST)
    candle_ts = pd.Timestamp("2026-11-17 10:00", tz=IST)
    candles = pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 1000.0]],
        index=pd.DatetimeIndex([candle_ts]),
        columns=["open", "high", "low", "close", "volume"],
    )

    class CandleClient:
        def intraday(self, key):
            return candles

    eng = Engine(CandleClient(), MemoryStore())
    state = SymbolState(
        SymbolInfo("TEST", "K", "normal", lot=100, underlying="TEST"),
        hist=pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
        warm_day=now.date(),
    )

    # At the exact close boundary, the six-second fetch delay has not elapsed.
    assert eng._load(state, now, "normal").empty
    # Once the configured delay has elapsed, the closed candle is eligible.
    loaded = eng._load(state, now + timedelta(seconds=6), "normal")
    assert len(loaded) == 1
    assert loaded.index[-1] == candle_ts

def test_weekend_and_empty_universe_safe():
    store = MemoryStore()
    eng = Engine(FakeClient(make_history(3), datetime(2026, 11, 14, 10, 0, tzinfo=IST)), store,
                 universe_builder=lambda n: Universe(built_on="2026-11-14"))
    eng.housekeeping(datetime(2026, 11, 14, 10, 0, tzinfo=IST))   # Saturday: no-op
    assert eng.run_boundary(datetime(2026, 11, 17, 3, 0, tzinfo=IST)) == {}


def test_memory_store_notification_outbox_retries_until_delivered():
    store = MemoryStore()
    row = _row()
    row.update({"module": "normal", "symbol": "TEST", "trade_day": row["bar_ts"].date(),
                "entry": 100.0, "sl": 99.0, "tp1": 101.2, "tp2": 102.2,
                "score": 80, "trig": "test", "details": {"telegram_text": "alert"}})
    sid = store.add_signal(row)
    assert sid is not None
    assert store.pending_notifications() == [{"id": sid, "text": "alert"}]
    store.mark_notification(sid, False, "temporary Telegram failure")
    assert store.pending_notifications() == [{"id": sid, "text": "alert"}]
    store.mark_notification(sid, True)
    assert store.pending_notifications() == []


def test_engine_retries_failed_telegram_delivery_without_duplicate_signal(monkeypatch):
    df, t = _find_signal_case()
    monkeypatch.setattr(mod_normal, "evaluate", _always_normal_signal)
    monkeypatch.setattr(mod_normal, "setup_side", lambda f: 1)
    monkeypatch.setattr(mod_normal, "rsi_triggered", lambda f, side: f.bar_ts > pd.Timestamp(t).tz_convert(IST) + pd.Timedelta(minutes=5))
    now = t + timedelta(minutes=5, seconds=7)
    info = SymbolInfo("TEST", "K", "normal", lot=100, underlying="TEST")
    uni = _test_universe(info, now)
    calls, delivered = [0], []
    def flaky_notify(message):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("temporary Telegram outage")
        delivered.append(message)
        return True
    store = MemoryStore()
    eng = Engine(FakeClient(df, now), store, notify=flaky_notify, universe_builder=lambda n: uni)
    eng.ensure_universe(now)
    eng.cycle("normal", now)
    candle1_now = now + timedelta(minutes=5)
    eng.client.now = candle1_now
    armed = eng.cycle("normal", candle1_now)
    assert armed["signals"] == 0 and armed["watching"] == 1
    candle2_now = now + timedelta(minutes=10)
    eng.client.now = candle2_now
    first = eng.cycle("normal", candle2_now)
    assert first["signals"] == 0
    assert len(store.pending_notifications()) == 1
    eng.last_outbox_retry_at = candle2_now - timedelta(minutes=5)
    second = eng.cycle("normal", candle2_now)
    assert len(delivered) == 1
    assert store.pending_notifications() == []
    assert second["signals"] == 0

def test_make_store_does_not_silently_fallback_when_database_url_is_set(monkeypatch):
    import app.db as db
    def unavailable(_dsn):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(db, "PostgresStore", unavailable)
    with pytest.raises(RuntimeError, match="database unavailable"):
        make_store("postgresql://unavailable")


def test_tracker_tp1_on_final_bar_closes_eod_with_half_position_mark_to_market():
    final_bar = _bars([(100, 101.3, 100.4, 101.0)])
    final_bar.index = pd.DatetimeIndex([pd.Timestamp("2026-11-17 15:10", tz=IST)])
    status, result = simulate(_row(), final_bar, time(15, 15))
    assert status == "EOD"
    assert result == pytest.approx(0.5 * 1.2 + 0.5 * 1.0)


def test_tracker_tp1_same_bar_breakeven_is_conservative():
    bars = _bars([(100, 101.3, 99.9, 100.2)])
    status, result = simulate(_row(), bars, time(15, 15))
    assert status == "TP1_BE"
    assert result == pytest.approx(0.6)


def test_memory_store_terminal_outcome_and_expiry_set_closed_at():
    store = MemoryStore()
    row = _row()
    row.update({"module": "normal", "symbol": "TEST", "trade_day": row["bar_ts"].date(),
                "entry": 100.0, "sl": 99.0, "tp1": 101.2, "tp2": 102.2,
                "score": 80, "trig": "test", "details": {}})
    sid = store.add_signal(row)
    store.update_outcome(sid, "TP1", 0.6)
    assert store.open_signals(row["trade_day"])[0]["status"] == "TP1"
    store.update_outcome(sid, "TP2", 1.7)
    assert store.open_signals(row["trade_day"]) == []
    assert store.rows[0]["closed_at"] is not None
    sid2 = store.add_signal(row | {"symbol": "TEST2"})
    store.expire_old(row["trade_day"] + timedelta(days=1))
    assert store.rows[1]["status"] == "EXPIRED"
    assert store.rows[1]["closed_at"] is not None


def test_postgres_run_discards_broken_connection_and_retries_once():
    import psycopg2
    from app.db import PostgresStore

    class Cursor:
        def __init__(self, fail): self.fail = fail
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params):
            if self.fail: raise psycopg2.OperationalError("connection reset")
        def fetchone(self): return (42,)

    class Conn:
        def __init__(self, fail): self.fail, self.closed = fail, 0
        def cursor(self): return Cursor(self.fail)
        def commit(self): pass
        def rollback(self): pass

    class Pool:
        def __init__(self): self.items, self.closed = [Conn(True), Conn(False)], []
        def getconn(self): return self.items.pop(0)
        def putconn(self, conn, close=False): self.closed.append(close)

    store = PostgresStore.__new__(PostgresStore)
    store.pool = Pool()
    assert store._run("SELECT 42", fetch="one") == (42,)
    assert store.pool.closed == [True, False]


def test_dynamic_index_and_optionable_midcap_resolution():
    today = datetime(2026, 11, 17, tzinfo=IST)
    expiry = int((today + timedelta(days=10)).timestamp() * 1000)
    rows = []
    for symbol in ("MIDCAP", "NO_PUT"):
        rows.extend([
            {"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": symbol,
             "instrument_key": f"NSE_EQ|{symbol}"},
            {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_type": "EQUITY",
             "underlying_symbol": symbol, "expiry": expiry, "instrument_key": f"NSE_FO|{symbol}FUT",
             "lot_size": 50, "trading_symbol": f"{symbol} FUT"},
            {"segment": "NSE_FO", "instrument_type": "CE", "underlying_type": "EQUITY",
             "underlying_symbol": symbol, "expiry": expiry, "strike_price": 1000,
             "instrument_key": f"NSE_FO|{symbol}CE", "lot_size": 50, "trading_symbol": f"{symbol} CE"},
        ])
    rows.append({"segment": "NSE_FO", "instrument_type": "PE", "underlying_type": "EQUITY",
                 "underlying_symbol": "MIDCAP", "expiry": expiry, "strike_price": 1000,
                 "instrument_key": "NSE_FO|MIDCAPPE", "lot_size": 50, "trading_symbol": "MIDCAP PE"})
    rows.extend([
        {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_type": "INDEX",
         "underlying_symbol": "NIFTY", "expiry": expiry, "instrument_key": "NSE_FO|NIFTYFUT",
         "lot_size": 75, "trading_symbol": "NIFTY FUT"},
        {"segment": "NSE_FO", "instrument_type": "CE", "underlying_type": "INDEX",
         "underlying_symbol": "NIFTY", "expiry": expiry, "strike_price": 26000,
         "instrument_key": "NSE_FO|NIFTYCE", "lot_size": 75, "trading_symbol": "NIFTY CE"},
        {"segment": "NSE_FO", "instrument_type": "PE", "underlying_type": "INDEX",
         "underlying_symbol": "NIFTY", "expiry": expiry, "strike_price": 26000,
         "instrument_key": "NSE_FO|NIFTYPE", "lot_size": 75, "trading_symbol": "NIFTY PE"},
    ])
    uni = resolve_nse(rows, today, ["MIDCAP", "NO_PUT"], [], require_options=True)
    names = {item.symbol for item in uni.normal}
    assert names == {"MIDCAP", "NIFTY"}
    assert next(item for item in uni.normal if item.symbol == "NIFTY").is_index
    assert {option.otype for option in uni.options["MIDCAP"]} == {"CE", "PE"}


def test_simple_call_put_levels_are_atr_based():
    df, ts = _find_signal_case()
    ind = add_indicators(df.loc[:ts])
    features = compute_features(ind)
    info = SymbolInfo("TEST", "K", "normal", lot=100)
    signal, reason = mod_normal.evaluate(info, ind, features, Ctx())
    assert reason is None and signal is not None
    raw_risk = 1.2 * features.atr
    assert signal.risk == pytest.approx(raw_risk, rel=0.01)
    # Targets are calculated from the unrounded ATR risk, then each price is
    # rounded independently to the instrument tick. Compare to the raw formula
    # with one tick of absolute tolerance instead of reusing the rounded SL risk.
    assert abs(abs(signal.tp1 - signal.entry) - 1.5 * raw_risk) <= info.tick
    assert abs(abs(signal.tp2 - signal.entry) - 2.0 * raw_risk) <= info.tick


def test_dispatch_batches_optional_option_quotes_once(monkeypatch):
    monkeypatch.setattr("app.engine.OPTION_QUOTE", True)
    now = datetime(2026, 11, 17, 10, 30, tzinfo=IST)
    infos = [
        SymbolInfo("AAA", "NSE_EQ|AAA", "normal", lot=10, underlying="AAA"),
        SymbolInfo("BBB", "NSE_EQ|BBB", "normal", lot=10, underlying="BBB"),
    ]
    expiry = now + timedelta(days=10)
    options = {}
    signals = []
    for i, info in enumerate(infos):
        key = f"NSE_FO|{info.symbol}CE"
        options[info.underlying] = [
            OptionRec(expiry, 100.0, "CE", key, f"{info.symbol} CE", 10),
        ]
        signals.append(Signal(
            "normal", info.symbol, "LONG",
            pd.Timestamp("2026-11-17 10:20", tz=IST),
            100.0, 98.0, 103.0, 104.0, 1.0, 0, "ema_vwap",
            ["test"], info,
        ))

    client = FakeClient(make_history(3), now)
    quote_calls = []
    def batch_ltp(keys):
        quote_calls.append(list(keys))
        return {key: 12.5 + i for i, key in enumerate(keys)}
    client.ltp = batch_ltp

    sent = []
    store = MemoryStore()
    eng = Engine(client, store, notify=lambda message: sent.append(message))
    eng.uni = Universe(normal=infos, options=options, built_on=now.strftime("%Y-%m-%d"))
    diag = {"rejects": Counter()}

    count = eng._dispatch("normal", NORMAL, signals, now, False, diag)

    assert count == 2
    assert len(quote_calls) == 1
    assert set(quote_calls[0]) == {"NSE_FO|AAACE", "NSE_FO|BBBCE"}
    assert len(sent) == 2
    assert all("LTP" in message for message in sent)
