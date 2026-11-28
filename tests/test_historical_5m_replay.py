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
    assert results[0]["option_type"] == "CALL"
    assert results[0]["status"] == "TP2"
    assert results[0]["net_r"] == 2.0
    assert prefix_lengths == list(range(50, 53))
    assert all(size <= len(frame) for size in prefix_lengths)


def test_warmup_history_requires_distinct_sessions_before_evaluation_date():
    dates = []
    for day in pd.date_range("2026-10-01", periods=6, freq="B"):
        dates.extend(pd.date_range(day.replace(hour=9, minute=15), periods=2, freq="5min", tz="Asia/Kolkata"))
    frame = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}, index=pd.DatetimeIndex(dates))
    replay.validate_warmup_history(frame, pd.Timestamp("2026-10-12").date(), 6)
    with pytest.raises(ValueError, match="found 6 prior sessions, require 7"):
        replay.validate_warmup_history(frame, pd.Timestamp("2026-10-12").date(), 7)


def test_warmup_history_rejects_zero_sessions():
    with pytest.raises(ValueError, match="positive integer"):
        replay.validate_warmup_history(_bars(), pd.Timestamp("2026-11-03").date(), 0)


def test_replay_frame_excludes_warmup_signals_from_results(monkeypatch):
    first = _bars(52)
    second = _bars(52)
    second.index = second.index + pd.Timedelta(days=1)
    frame = pd.concat([first, second])
    monkeypatch.setattr(replay, "add_indicators", lambda data: data)
    monkeypatch.setattr(replay, "compute_features", lambda data: SimpleNamespace(bar_ts=data.index[-1]))
    monkeypatch.setattr(replay.mod_normal, "setup_side", lambda f: 1)
    monkeypatch.setattr(replay.mod_normal, "rsi_triggered", lambda f, side: True)
    monkeypatch.setattr(replay.mod_normal, "evaluate", lambda info, data, f, ctx: (
        SimpleNamespace(side="LONG", bar_ts=f.bar_ts, entry=100.0, sl=98.0,
                         tp1=103.0, tp2=104.0, atr=1.0, score=80), None
    ))
    monkeypatch.setattr(replay, "simulate", lambda row, data, session_end: ("TP2", 2.0))
    results = replay.replay_frame("ABC", frame, evaluation_from_date=second.index[0].date())
    assert results
    assert all(pd.Timestamp(row["bar_ts"]).date() >= second.index[0].date() for row in results)


def test_empty_replay_returns_no_signals():
    assert replay.replay_frame("ABC", _bars(0)) == []



def test_load_tick_sizes_validates_and_normalizes_symbols(tmp_path):
    path = tmp_path / "ticks.csv"
    path.write_text("symbol,tick_size\nabc,0.01\nXYZ,0.05\n", encoding="utf-8")
    assert replay.load_tick_sizes(path) == {"ABC": 0.01, "XYZ": 0.05}


@pytest.mark.parametrize(
    "content, message",
    [
        ("symbol,tick_size\nABC,0\n", "positive finite"),
        ("symbol,tick_size\nABC,nan\n", "positive finite"),
        ("symbol,tick_size\nABC,0.05\nABC,0.01\n", "duplicate"),
        ("symbol,price_tick\nABC,0.05\n", "must contain"),
    ],
)
def test_load_tick_sizes_rejects_invalid_mappings(tmp_path, content, message):
    path = tmp_path / "ticks.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        replay.load_tick_sizes(path)


def test_replay_directory_uses_each_symbols_tick_size(monkeypatch, tmp_path):
    input_dir = tmp_path / "data"
    input_dir.mkdir()
    (input_dir / "ABC.csv").write_text("placeholder", encoding="utf-8")
    (input_dir / "XYZ.csv").write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(replay, "load_ohlcv", lambda path: _bars(1))
    seen = []
    monkeypatch.setattr(
        replay, "replay_frame",
        lambda symbol, frame, tick: seen.append((symbol, tick)) or [],
    )
    replay.replay_directory(
        input_dir, tmp_path / "out.csv", tick_sizes={"ABC": 0.01, "XYZ": 0.05}
    )
    assert seen == [("ABC", 0.01), ("XYZ", 0.05)]


def test_replay_directory_fails_closed_when_tick_mapping_is_missing(monkeypatch, tmp_path):
    input_dir = tmp_path / "data"
    input_dir.mkdir()
    (input_dir / "ABC.csv").write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(replay, "load_ohlcv", lambda path: _bars(1))
    with pytest.raises(ValueError, match="no tick_size mapping for ABC"):
        replay.replay_directory(
            input_dir, tmp_path / "out.csv", tick_sizes={"XYZ": 0.05}
        )



def test_quality_gate_accepts_exact_pass_symbol_set(tmp_path):
    files = [tmp_path / "abc.csv", tmp_path / "XYZ.csv"]
    summary = tmp_path / "symbol_summary.csv"
    summary.write_text(
        "symbol,status,rows\nABC,PASS,150\nXYZ,PASS,150\n",
        encoding="utf-8",
    )
    replay.validate_quality_summary(files, summary)


@pytest.mark.parametrize(
    "rows, message",
    [
        ("symbol,status\nABC,REVIEW\nXYZ,PASS\n", "must have PASS"),
        ("symbol,status\nABC,PASS\n", "symbol set mismatch"),
        ("symbol,status\nABC,PASS\nXYZ,PASS\nEXTRA,PASS\n", "symbol set mismatch"),
        ("symbol,status\nABC,PASS\nABC,PASS\nXYZ,PASS\n", "duplicate"),
        ("symbol,status\n,PASS\nXYZ,PASS\n", "empty symbol"),
    ],
)
def test_quality_gate_rejects_non_pass_or_ambiguous_summary(tmp_path, rows, message):
    files = [tmp_path / "abc.csv", tmp_path / "XYZ.csv"]
    summary = tmp_path / "symbol_summary.csv"
    summary.write_text(rows, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        replay.validate_quality_summary(files, summary)


def test_replay_directory_runs_quality_gate_before_loading_data(monkeypatch, tmp_path):
    input_dir = tmp_path / "data"
    input_dir.mkdir()
    (input_dir / "ABC.csv").write_text("not read", encoding="utf-8")
    summary = tmp_path / "symbol_summary.csv"
    summary.write_text("symbol,status\nABC,REVIEW\n", encoding="utf-8")
    monkeypatch.setattr(
        replay, "load_ohlcv",
        lambda path: pytest.fail("OHLCV must not load when quality gate rejects"),
    )
    with pytest.raises(ValueError, match="must have PASS"):
        replay.replay_directory(
            input_dir, tmp_path / "out.csv", quality_summary=summary
        )
