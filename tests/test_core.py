import gzip, io, json, os, sys, warnings
from datetime import datetime, time, timedelta

os.environ["AUTOSTART"] = "0"
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.common import Ctx, SymbolInfo
from app.config import IST
from app.db import MemoryStore, make_store
from app.engine import Engine
from app.indicators import add_indicators, compute_features, rvol_time_of_day
from app.tracker import simulate
from app.universe import Universe, resolve_nse, resolve_mcx
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
    assert "RES" in names and u.dropped["normal"] == ["FINNIFTY", "GONE"]


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


def test_engine_end_to_end_dedupe_and_cooldown():
    df, t = _find_signal_case()
    now = t + timedelta(minutes=5, seconds=7)          # just after the candle closed
    info = SymbolInfo("TEST", "K", "normal", lot=100, underlying="TEST")
    uni = Universe(normal=[info], built_on=now.strftime("%Y-%m-%d"))
    sent, store = [], MemoryStore()
    eng = Engine(FakeClient(df, now), store, notify=lambda text, silent=False: sent.append(text),
                 universe_builder=lambda n: uni)
    eng.bias = "neutral"
    eng.ensure_universe(now)
    out = eng.cycle("normal", now)
    assert out["signals"] == 1 and len(sent) == 1
    assert "TEST" in sent[0] and "<b>" in sent[0] and ("🟢" in sent[0] or "🔴" in sent[0])
    out2 = eng.cycle("normal", now)                    # same candle again -> no duplicate alert
    assert out2["signals"] == 0 and len(sent) == 1
    # a stale view (3 bars later with no new data) must not signal
    late = now + timedelta(minutes=15)
    eng.client.now = now
    out3 = eng.cycle("normal", late)
    assert out3["signals"] == 0



def test_force_run_still_rejects_stale_market_data(monkeypatch):
    df, t = _find_signal_case()
    original_now = t + timedelta(minutes=5, seconds=7)
    late_now = original_now + timedelta(minutes=15)
    info = SymbolInfo("TEST", "K", "normal", lot=100, underlying="TEST")
    uni = Universe(normal=[info], built_on=late_now.strftime("%Y-%m-%d"))
    sent = []
    eng = Engine(FakeClient(df, original_now), MemoryStore(),
                 notify=lambda message: sent.append(message),
                 universe_builder=lambda n: uni)
    eng.ensure_universe(late_now)
    monkeypatch.setattr("app.engine._time.sleep", lambda seconds: None)

    out = eng.cycle("normal", late_now, force=True)

    assert out["signals"] == 0
    assert out["rejects"].get("stale_data", 0) == 1
    assert sent == []

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


def test_engine_rejects_previous_session_bias():
    now = datetime(2026, 11, 17, 10, 5, tzinfo=IST)
    eng = Engine(FakeClient(make_history(3), now), MemoryStore())
    previous = make_history(3, seed=19)
    previous = previous[previous.index.date < now.date()]
    assert eng._set_bias(previous, now) is False
    assert eng.bias == "neutral" and eng.bias_at is None


def test_engine_retries_failed_telegram_delivery_without_duplicate_signal():
    df, t = _find_signal_case()
    now = t + timedelta(minutes=5, seconds=7)
    info = SymbolInfo("TEST", "K", "normal", lot=100, underlying="TEST")
    uni = Universe(normal=[info], built_on=now.strftime("%Y-%m-%d"))
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
    first = eng.cycle("normal", now)
    assert first["signals"] == 0
    assert len(store.pending_notifications()) == 1
    # Simulate the five-minute retry interval without advancing the market candle.
    eng.last_outbox_retry_at = now - timedelta(minutes=5)
    second = eng.cycle("normal", now)
    assert len(delivered) == 1
    assert store.pending_notifications() == []
    # The signal is suppressed by the active cooldown after the outbox retry.
    assert second["rejects"].get("cooldown", 0) == 1


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
