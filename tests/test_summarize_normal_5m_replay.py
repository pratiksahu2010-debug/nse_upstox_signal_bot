import csv

import pytest

from scripts import summarize_normal_5m_replay as summary


def _write(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["symbol", "bar_ts", "option_type", "status", "net_r"]
        )
        writer.writeheader()
        writer.writerows(rows)


def _row(
    symbol="ABC",
    stamp="2026-11-02T09:30:00+05:30",
    status="TP2",
    net_r="2",
    option_type="CALL",
):
    return {
        "symbol": symbol,
        "bar_ts": stamp,
        "option_type": option_type,
        "status": status,
        "net_r": net_r,
    }


def test_report_computes_closed_trade_metrics_and_excludes_open(tmp_path):
    path = tmp_path / "replay.csv"
    _write(path, [
        _row(net_r="2"),
        _row(stamp="2026-11-02T09:35:00+05:30", status="SL", net_r="-1"),
        _row(stamp="2026-11-02T09:40:00+05:30", status="OPEN", net_r=""),
        _row("XYZ", "2026-11-02T09:30:00+05:30", "TP1_BE", "0.5"),
    ])
    rows = summary.load_replay(path)
    report = summary.build_report(rows)
    all_row = report[0]
    assert all_row["group_type"] == "overall"
    assert all_row["signals"] == 4
    assert all_row["closed"] == 3
    assert all_row["open"] == 1
    assert all_row["wins"] == 2
    assert all_row["losses"] == 1
    assert all_row["win_rate_pct"] == 66.6667
    assert all_row["total_net_r"] == 1.5
    assert all_row["max_drawdown_r"] == 1.0
    assert {row["group"] for row in report if row["group_type"] == "symbol"} == {"ABC", "XYZ"}
    session_rows = [row for row in report if row["group_type"] == "session"]
    assert len(session_rows) == 1
    assert session_rows[0]["group"] == "2026-11-02"
    assert session_rows[0]["signals"] == 4


def test_session_groups_use_ist_calendar_date(tmp_path):
    path = tmp_path / "replay.csv"
    _write(path, [
        _row(stamp="2026-11-01T18:45:00+00:00", net_r="1"),
        _row(stamp="2026-11-02T03:00:00+00:00", net_r="-1"),
    ])
    rows = summary.load_replay(path)
    session_rows = [row for row in summary.build_report(rows) if row["group_type"] == "session"]
    assert [row["group"] for row in session_rows] == ["2026-11-02"]
    assert session_rows[0]["signals"] == 2
    assert session_rows[0]["total_net_r"] == 0.0


@pytest.mark.parametrize(
    "rows, message",
    [
        ([_row(status="OPEN", net_r="0.2")], "OPEN row"),
        ([_row(status="TP2", net_r="nan")], "finite"),
        ([_row(status="TP2", net_r="")], "numeric net_r"),
        ([_row(option_type="LONG")], "option_type"),
        ([_row(stamp="2026-11-02T09:30:00")], "timezone"),
        ([_row(), _row()], "duplicate"),
        ([_row(status="UNKNOWN")], "invalid status"),
    ],
)
def test_loader_rejects_ambiguous_or_invalid_rows(tmp_path, rows, message):
    path = tmp_path / "replay.csv"
    _write(path, rows)
    with pytest.raises(ValueError, match=message):
        summary.load_replay(path)


def test_loader_rejects_empty_input(tmp_path):
    path = tmp_path / "replay.csv"
    _write(path, [])
    with pytest.raises(ValueError, match="no signal rows"):
        summary.load_replay(path)


def test_write_report_creates_parent_and_csv(tmp_path):
    input_path = tmp_path / "replay.csv"
    _write(input_path, [_row()])
    rows = summary.load_replay(input_path)
    output_path = tmp_path / "nested" / "summary.csv"
    report = summary.write_report(rows, output_path)
    assert output_path.exists()
    assert report[0]["group"] == "ALL"
    assert report[0]["closed"] == 1
    with output_path.open(newline="", encoding="utf-8") as handle:
        assert "group_type" in next(csv.reader(handle))
