from datetime import date

import pandas as pd
import pytest

from scripts.download_historical_5m import (
    download_one,
    monthly_chunks,
    read_instruments,
    write_atomic,
)


def test_monthly_chunks_cover_range_without_gaps_or_overlap():
    chunks = list(monthly_chunks(date(2026, 5, 17), date(2026, 10, 4)))
    assert chunks == [
        (date(2026, 5, 17), date(2026, 5, 31)),
        (date(2026, 6, 1), date(2026, 6, 30)),
        (date(2026, 7, 1), date(2026, 7, 31)),
        (date(2026, 8, 1), date(2026, 8, 31)),
        (date(2026, 9, 1), date(2026, 9, 30)),
        (date(2026, 10, 1), date(2026, 10, 4)),
    ]


def test_monthly_chunks_reject_reversed_dates():
    with pytest.raises(ValueError, match="on or before"):
        list(monthly_chunks(date(2026, 6, 1), date(2026, 5, 31)))


def test_read_instruments_requires_unique_explicit_keys(tmp_path):
    path = tmp_path / "instruments.csv"
    path.write_text(
        "symbol,instrument_key\nRELIANCE,NSE_EQ|INE002A01018\nSBIN,NSE_EQ|INE062A01020\n",
        encoding="utf-8",
    )
    assert read_instruments(path) == [
        ("RELIANCE", "NSE_EQ|INE002A01018"),
        ("SBIN", "NSE_EQ|INE062A01020"),
    ]

    path.write_text(
        "symbol,instrument_key\\nM&M,NSE_EQ|INE296A01024\\nGVT&D,NSE_EQ|INE123A01016\\n",
        encoding="utf-8",
    )
    assert read_instruments(path) == [
        ("M&M", "NSE_EQ|INE296A01024"),
        ("GVT&D", "NSE_EQ|INE123A01016"),
    ]

    path.write_text(
        "symbol,instrument_key\nRELIANCE,key-1\nRELIANCE,key-2\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        read_instruments(path)


def test_download_one_fetches_each_month_as_five_minute_and_deduplicates():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def historical(self, key, start, end, interval):
            self.calls.append((key, start, end, interval))
            index = pd.DatetimeIndex(
                [pd.Timestamp("2026-05-29 15:25", tz="Asia/Kolkata"),
                 pd.Timestamp("2026-06-01 09:15", tz="Asia/Kolkata")]
            )
            return pd.DataFrame(
                {"open": [100, 101], "high": [102, 103], "low": [99, 100],
                 "close": [101, 102], "volume": [10, 20], "oi": [None, None]},
                index=index,
            )

    client = FakeClient()
    result = download_one(client, "NSE_EQ|KEY", date(2026, 5, 29), date(2026, 6, 2))
    assert len(client.calls) == 2
    assert all(call[0] == "NSE_EQ|KEY" and call[3] == 5 for call in client.calls)
    assert len(result) == 2
    assert result.index.is_monotonic_increasing


def test_write_atomic_produces_timestamped_csv(tmp_path):
    index = pd.DatetimeIndex([pd.Timestamp("2026-05-29 09:15", tz="Asia/Kolkata")])
    frame = pd.DataFrame(
        {"open": [100], "high": [102], "low": [99], "close": [101],
         "volume": [10], "oi": [None]},
        index=index,
    )
    target = tmp_path / "RELIANCE.csv"
    write_atomic(frame, target)
    saved = pd.read_csv(target)
    assert saved.columns.tolist() == [
        "timestamp", "open", "high", "low", "close", "volume", "oi"
    ]
    assert saved.iloc[0]["close"] == 101
    assert not (tmp_path / "RELIANCE.csv.tmp").exists()
