from datetime import date

import pandas as pd

from scripts.audit_historical_5m import (
    audit_directory,
    audit_file,
    expected_session_index,
    read_calendar,
)


def _write_bars(path, timestamps):
    frame = pd.DataFrame({
        "timestamp": [ts.isoformat() for ts in timestamps],
        "open": [100.0] * len(timestamps),
        "high": [101.0] * len(timestamps),
        "low": [99.0] * len(timestamps),
        "close": [100.5] * len(timestamps),
        "volume": [1000] * len(timestamps),
    })
    frame.to_csv(path, index=False)


def test_expected_session_has_75_five_minute_bars():
    index = expected_session_index(date(2026, 11, 2))
    assert len(index) == 75
    assert index[0].strftime("%H:%M") == "09:15"
    assert index[-1].strftime("%H:%M") == "15:25"


def test_audit_marks_complete_session_pass(tmp_path):
    path = tmp_path / "ABC.csv"
    _write_bars(path, expected_session_index(date(2026, 11, 2)))
    summary, sessions = audit_file(path)
    assert summary["status"] == "PASS"
    assert summary["complete_sessions"] == 1
    assert summary["missing_bars"] == 0
    assert sessions[0]["status"] == "COMPLETE"
    assert sessions[0]["observed_bars"] == 75


def test_audit_detects_missing_and_duplicate_bars(tmp_path):
    path = tmp_path / "ABC.csv"
    bars = list(expected_session_index(date(2026, 11, 2)))
    bars.remove(bars[20])
    bars.append(bars[0])
    _write_bars(path, bars)
    summary, sessions = audit_file(path)
    assert summary["status"] == "REVIEW"
    assert summary["missing_bars"] == 1
    assert summary["duplicate_timestamps"] == 1
    assert sessions[0]["status"] == "INCOMPLETE"


def test_calendar_exposes_entirely_missing_session(tmp_path):
    path = tmp_path / "ABC.csv"
    _write_bars(path, expected_session_index(date(2026, 11, 2)))
    calendar_path = tmp_path / "calendar.csv"
    calendar_path.write_text(
        "date\n2026-11-02\n2026-11-03\n", encoding="utf-8"
    )
    calendar = read_calendar(calendar_path)
    summary, sessions = audit_file(path, calendar)
    assert [row["status"] for row in sessions] == ["COMPLETE", "NO_DATA"]
    assert summary["status"] == "REVIEW"
    assert summary["trading_days"] == 1


def test_empty_download_is_reported_as_no_data(tmp_path):
    path = tmp_path / "EMPTY.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume,oi\n", encoding="utf-8"
    )
    summary, sessions = audit_file(path)
    assert summary["status"] == "NO_DATA"
    assert summary["rows"] == 0
    assert sessions == []


def test_audit_directory_writes_both_reports(tmp_path):
    source = tmp_path / "data"
    output = tmp_path / "reports"
    source.mkdir()
    _write_bars(source / "ABC.csv", expected_session_index(date(2026, 11, 2)))
    summaries, sessions = audit_directory(source, output)
    assert summaries[0]["symbol"] == "ABC"
    assert len(sessions) == 1
    assert (output / "symbol_summary.csv").exists()
    assert (output / "session_detail.csv").exists()


def test_date_filter_with_no_matching_rows_is_not_pass(tmp_path):
    path = tmp_path / "ABC.csv"
    _write_bars(path, expected_session_index(date(2026, 11, 2)))
    summary, sessions = audit_file(
        path, start_date=date(2026, 11, 3), end_date=date(2026, 11, 4)
    )
    assert summary["status"] == "NO_DATA"
    assert summary["trading_days"] == 0
    assert sessions == []


import pytest


@pytest.mark.parametrize(
    "column,value",
    [
        ("open", "not-a-price"),
        ("high", 99.0),
        ("low", 102.0),
        ("close", float("inf")),
        ("volume", -1),
    ],
)
def test_audit_flags_invalid_ohlcv_rows(tmp_path, column, value):
    path = tmp_path / "ABC.csv"
    frame = pd.DataFrame({
        "timestamp": [ts.isoformat() for ts in expected_session_index(date(2026, 11, 2))],
        "open": [100.0] * 75,
        "high": [101.0] * 75,
        "low": [99.0] * 75,
        "close": [100.5] * 75,
        "volume": [1000] * 75,
    })
    frame.loc[10, column] = value
    frame.to_csv(path, index=False)

    summary, sessions = audit_file(path)

    assert summary["status"] == "REVIEW"
    assert summary["invalid_ohlcv_rows"] == 1
    assert sessions[0]["status"] == "INCOMPLETE"
    assert sessions[0]["invalid_ohlcv_rows"] == 1
