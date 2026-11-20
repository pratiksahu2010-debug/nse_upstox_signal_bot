from datetime import datetime, timedelta

from app.config import IST
from app.universe import OptionRec, Universe, nearby_options


def test_nearby_options_uses_nearest_eligible_expiry_and_correct_side():
    today = datetime(2026, 11, 19, tzinfo=IST)
    near = today + timedelta(days=7)
    far = today + timedelta(days=14)
    uni = Universe(options={
        "RELIANCE": [
            OptionRec(far, 100.0, "CE", "far", "FARCE", 250),
            OptionRec(near, 100.0, "CE", "atm", "ATMCE", 250),
            OptionRec(near, 105.0, "CE", "up", "UPCE", 250),
            OptionRec(near, 95.0, "CE", "down", "DOWNCE", 250),
            OptionRec(near, 100.0, "PE", "put", "ATMPE", 250),
        ]
    })

    calls = nearby_options(uni, "RELIANCE", 101.0, "LONG", today, candidate_count=3)
    puts = nearby_options(uni, "RELIANCE", 101.0, "SHORT", today, candidate_count=3)

    assert [item.key for item in calls] == ["atm", "up", "down"]
    assert [item.key for item in puts] == ["put"]


def test_nearby_options_returns_empty_when_no_eligible_contracts():
    today = datetime(2026, 11, 19, tzinfo=IST)
    expired = today - timedelta(days=1)
    uni = Universe(options={
        "RELIANCE": [OptionRec(expired, 100.0, "CE", "old", "OLDCE", 250)]
    })

    assert nearby_options(uni, "RELIANCE", 100.0, "LONG", today) == []
