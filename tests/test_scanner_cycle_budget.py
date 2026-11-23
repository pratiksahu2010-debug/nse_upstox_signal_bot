from app.web import _cycle_duration_seconds


def test_cycle_duration_seconds_handles_offset_aware_timestamps():
    duration = _cycle_duration_seconds(
        "2026-11-23T11:49:16+05:30",
        "2026-11-23T11:57:48+05:30",
    )
    assert duration == 512.0


def test_cycle_duration_seconds_handles_naive_timestamps_as_ist():
    duration = _cycle_duration_seconds(
        "2026-11-23T11:49:16",
        "2026-11-23T11:57:48",
    )
    assert duration == 512.0


def test_cycle_duration_seconds_rejects_missing_or_invalid_timestamps():
    assert _cycle_duration_seconds(None, "2026-11-23T11:57:48+05:30") is None
    assert _cycle_duration_seconds("bad", "2026-11-23T11:57:48+05:30") is None
