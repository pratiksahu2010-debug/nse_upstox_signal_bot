from datetime import datetime

import pandas as pd

from app.config import IST
from app.engine import dispatch_bar_is_fresh, latest_eligible_bar


def test_dispatch_accepts_only_latest_completed_five_minute_bar():
    now = datetime(2026, 11, 23, 11, 57, 10, tzinfo=IST)
    expected = latest_eligible_bar(now)

    assert expected == pd.Timestamp("2026-11-23T11:50:00+05:30")
    assert dispatch_bar_is_fresh(expected, now)


def test_dispatch_rejects_candidate_that_became_stale_during_scan():
    scan_started = datetime(2026, 11, 23, 11, 52, 10, tzinfo=IST)
    dispatch_time = datetime(2026, 11, 23, 11, 57, 10, tzinfo=IST)
    candidate_bar = latest_eligible_bar(scan_started)

    assert candidate_bar == pd.Timestamp("2026-11-23T11:45:00+05:30")
    assert not dispatch_bar_is_fresh(candidate_bar, dispatch_time)


def test_dispatch_rejects_incomplete_or_malformed_bar_timestamp():
    now = datetime(2026, 11, 23, 11, 57, 10, tzinfo=IST)

    assert not dispatch_bar_is_fresh("not-a-timestamp", now)
