"""Audit downloaded 5-minute OHLCV files before historical replay.

The audit is read-only. Without an exchange-session calendar it evaluates only
dates that contain at least one candle; supply --calendar to detect fully
missing sessions without mistaking exchange holidays for data gaps.
"""
import argparse
import csv
import logging
import sys
from datetime import date, time
from pathlib import Path

import pandas as pd

log = logging.getLogger("historical_5m_quality")
SESSION_START = time(9, 15)
SESSION_LAST_BAR = time(15, 25)
BAR_MINUTES = 5
EXPECTED_BARS = 75
SUMMARY_COLUMNS = [
    "symbol", "status", "rows", "trading_days", "complete_sessions",
    "incomplete_sessions", "missing_bars", "duplicate_timestamps",
    "invalid_timestamps", "invalid_ohlcv_rows", "off_session_bars", "first_timestamp",
    "last_timestamp",
]
SESSION_COLUMNS = [
    "symbol", "session_date", "status", "observed_bars",
    "missing_bars", "duplicate_timestamps", "invalid_ohlcv_rows", "off_session_bars",
    "first_timestamp", "last_timestamp",
]


def expected_session_index(session_date: date) -> pd.DatetimeIndex:
    """Return the 75 expected 5-minute candle start times for a regular NSE day."""
    return pd.date_range(
        pd.Timestamp.combine(session_date, SESSION_START).tz_localize("Asia/Kolkata"),
        pd.Timestamp.combine(session_date, SESSION_LAST_BAR).tz_localize("Asia/Kolkata"),
        freq=f"{BAR_MINUTES}min",
    )


def read_calendar(path: Path) -> list[date]:
    """Read an explicit trading-session calendar CSV with a single 'date' column."""
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "date" not in reader.fieldnames:
            raise ValueError("calendar CSV must contain a date column (YYYY-MM-DD)")
        values = []
        for line, row in enumerate(reader, start=2):
            raw = (row.get("date") or "").strip()
            try:
                values.append(date.fromisoformat(raw))
            except ValueError as exc:
                raise ValueError(f"invalid calendar date on line {line}: {raw!r}") from exc
    if len(values) != len(set(values)):
        raise ValueError("calendar contains duplicate dates")
    return sorted(values)


def audit_file(
    path: Path,
    expected_sessions: list[date] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> tuple[dict, list[dict]]:
    """Return a symbol summary and per-session detail for one downloaded CSV."""
    symbol = path.stem.upper()
    raw = pd.read_csv(path)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing_columns = required - set(raw.columns)
    if missing_columns:
        raise ValueError(f"{path}: missing columns {sorted(missing_columns)}")

    raw_count = len(raw)
    parsed = pd.to_datetime(raw["timestamp"], errors="coerce")
    invalid_timestamps = int(parsed.isna().sum())
    valid = parsed.notna()
    parsed = parsed.loc[valid]
    if parsed.dt.tz is None:
        parsed = parsed.dt.tz_localize("Asia/Kolkata")
    else:
        parsed = parsed.dt.tz_convert("Asia/Kolkata")

    frame = raw.loc[valid].copy()
    frame["timestamp"] = parsed
    duplicate_timestamps = int(frame["timestamp"].duplicated(keep="last").sum())

    if start_date is not None:
        frame = frame.loc[frame["timestamp"].dt.date >= start_date]
    if end_date is not None:
        frame = frame.loc[frame["timestamp"].dt.date <= end_date]

    ohlcv_columns = ["open", "high", "low", "close", "volume"]
    numeric = frame[ohlcv_columns].apply(pd.to_numeric, errors="coerce")
    finite = pd.DataFrame(
        {column: pd.Series(pd.notna(numeric[column]) & numeric[column].map(
            lambda value: bool(pd.notna(value)) and abs(float(value)) != float("inf")
        ), index=numeric.index) for column in ohlcv_columns},
        index=numeric.index,
    ).all(axis=1)
    valid_ohlcv = finite.copy()
    valid_ohlcv &= numeric["volume"] >= 0
    valid_ohlcv &= numeric["high"] >= numeric[["open", "close"]].max(axis=1)
    valid_ohlcv &= numeric["low"] <= numeric[["open", "close"]].min(axis=1)
    frame["__invalid_ohlcv"] = ~valid_ohlcv

    if expected_sessions is None:
        session_dates = sorted(set(frame["timestamp"].dt.date))
    else:
        session_dates = [
            day for day in expected_sessions
            if (start_date is None or day >= start_date)
            and (end_date is None or day <= end_date)
        ]

    session_rows = []
    for day in session_dates:
        day_frame = frame.loc[frame["timestamp"].dt.date == day]
        expected = expected_session_index(day)
        actual = pd.DatetimeIndex(day_frame["timestamp"].drop_duplicates())
        on_session = actual.intersection(expected)
        missing_count = len(expected.difference(on_session))
        off_session_count = int((~day_frame["timestamp"].isin(expected)).sum())
        day_duplicates = int(day_frame["timestamp"].duplicated(keep="last").sum())
        day_invalid_ohlcv = int(day_frame["__invalid_ohlcv"].sum())
        first = day_frame["timestamp"].min() if not day_frame.empty else None
        last = day_frame["timestamp"].max() if not day_frame.empty else None

        if day_frame.empty:
            status = "NO_DATA"
        elif day_duplicates or day_invalid_ohlcv or off_session_count or missing_count:
            status = "INCOMPLETE"
        else:
            status = "COMPLETE"

        session_rows.append({
            "symbol": symbol,
            "session_date": day.isoformat(),
            "status": status,
            "observed_bars": int(len(day_frame)),
            "missing_bars": int(missing_count),
            "duplicate_timestamps": day_duplicates,
            "invalid_ohlcv_rows": day_invalid_ohlcv,
            "off_session_bars": off_session_count,
            "first_timestamp": first.isoformat() if first is not None else "",
            "last_timestamp": last.isoformat() if last is not None else "",
        })

    observed_days = [row for row in session_rows if row["observed_bars"] > 0]
    complete = sum(row["status"] == "COMPLETE" for row in session_rows)
    incomplete = sum(row["status"] == "INCOMPLETE" for row in session_rows)
    no_data = sum(row["status"] == "NO_DATA" for row in session_rows)
    total_missing = sum(row["missing_bars"] for row in session_rows)
    total_duplicates = int(frame["timestamp"].duplicated(keep="last").sum())
    total_off_session = sum(row["off_session_bars"] for row in session_rows)
    total_invalid_ohlcv = int(frame["__invalid_ohlcv"].sum())
    if raw_count == 0 or (frame.empty and invalid_timestamps == 0):
        status = "NO_DATA"
    elif invalid_timestamps or total_invalid_ohlcv or incomplete or no_data:
        status = "REVIEW"
    else:
        status = "PASS"

    first = frame["timestamp"].min() if not frame.empty else None
    last = frame["timestamp"].max() if not frame.empty else None
    summary = {
        "symbol": symbol,
        "status": status,
        "rows": int(raw_count),
        "trading_days": int(len(observed_days)),
        "complete_sessions": int(complete),
        "incomplete_sessions": int(incomplete),
        "missing_bars": int(total_missing),
        "duplicate_timestamps": int(total_duplicates),
        "invalid_timestamps": int(invalid_timestamps),
        "invalid_ohlcv_rows": total_invalid_ohlcv,
        "off_session_bars": int(total_off_session),
        "first_timestamp": first.isoformat() if first is not None else "",
        "last_timestamp": last.isoformat() if last is not None else "",
    }
    return summary, session_rows


def audit_directory(
    input_dir: Path,
    output_dir: Path,
    start_date: date | None = None,
    end_date: date | None = None,
    calendar: list[date] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Audit all CSVs and write symbol/session reports."""
    if start_date and end_date and start_date > end_date:
        raise ValueError("from-date must be on or before to-date")
    files = sorted(input_dir.glob("*.csv"))
    if not files:
        raise ValueError(f"no CSV files found in {input_dir}")

    summaries, sessions = [], []
    for path in files:
        log.info("Auditing %s", path.stem.upper())
        summary, details = audit_file(path, calendar, start_date, end_date)
        summaries.append(summary)
        sessions.extend(details)

    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(summaries, columns=SUMMARY_COLUMNS).to_csv(
        output_dir / "symbol_summary.csv", index=False
    )
    pd.DataFrame(sessions, columns=SESSION_COLUMNS).to_csv(
        output_dir / "session_detail.csv", index=False
    )
    return summaries, sessions


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("research_results/data_quality"))
    parser.add_argument("--from-date", type=date.fromisoformat)
    parser.add_argument("--to-date", type=date.fromisoformat)
    parser.add_argument("--calendar", type=Path,
                        help="optional CSV of actual NSE trading dates with a 'date' column")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        calendar = read_calendar(args.calendar) if args.calendar else None
        summaries, _ = audit_directory(
            args.input_dir, args.output_dir, args.from_date, args.to_date, calendar
        )
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        log.error("Data-quality audit stopped: %s", exc)
        return 1
    passed = sum(row["status"] == "PASS" for row in summaries)
    reviewed = sum(row["status"] == "REVIEW" for row in summaries)
    empty = sum(row["status"] == "NO_DATA" for row in summaries)
    log.info("Audit complete: PASS=%d REVIEW=%d NO_DATA=%d", passed, reviewed, empty)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
