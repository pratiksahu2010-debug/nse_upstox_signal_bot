from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import replay_historical_5m as replay


def _bars(count=52):
    index = pd.date_range(
        "2026-11-02 09:15", periods=count, freq="5min", tz="Asia/Kolkata"
    )
    return pd.DataFrame(
        {
            "open": [100.0] * count,
            "high": [101.0] * count,
            "low": [99.0] * count,
            "close": [100.0] * count,
            "volume": [1000.0] * count,
        },
        index=index,
    )


def test_load_ohlcv_normalizes_timezone_and_validates_rows(tmp_path):
    path = tmp_path / "ABC.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2026-11-02 09:15:00,100,102,99,101,1000\n",
        encoding="utf-8",
    )
    loaded = replay.load_ohlcv(path)
    assert str(loaded.index.tz) == "Asia/Kolkata"
    assert loaded.iloc[0]["close"] == 101


def test_load_ohlcv_rejects_impossible_ohlc(tmp_path):
    path = tmp_path / "ABC.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2026-11-02 09:15:00,100,99,98,101,1000\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="high below"):
        replay.load_ohlcv(path)


def test_replay_is_causal_and_uses_normal_lifecycle(monkeypatch):
    frame = _bars()
    prefix_lengths = []
    monkeypatch.setattr(replay, "add_indicators", lambda data: data)

    def features(data):
        prefix_lengths.append(len(data))
        stamp = data.index[-1]
        return SimpleNamespace(bar_ts=stamp)

    monkeypatch.setattr(replay, "compute_features", features)
    monkeypatch.setattr(replay.mod_normal, "setup_side", lambda f: 1)
    monkeypatch.setattr(replay.mod_normal, "rsi_triggered", lambda f, side: True)

    def evaluate(info, data, f, ctx):
        assert ctx.ignore_time is False
        assert data.index[-1] == f.bar_ts
        return SimpleNamespace(
            side="LONG", bar_ts=f.bar_ts, entry=100.0, sl=98.0,
            tp1=103.0, tp2=104.0, atr=1.0, score=80,
        ), None

    monkeypatch.setattr(replay.mod_normal, "evaluate", evaluate)
    monkeypatch.setattr(replay, "simulate", lambda row, data, session_end: ("TP2", 2.0))

    results = replay.replay_frame("ABC", frame)
    assert len(results) == 1
    assert results[0]["status"] == "TP2"
    assert results[0]["net_r"] == 2.0
    assert prefix_lengths == list(range(50, 53))
    assert all(size <= len(frame) for size in prefix_lengths)


def test_empty_replay_returns_no_signals():
    assert replay.replay_frame("ABC", _bars(0)) == []
