from datetime import datetime, timezone

import pytest

from app.db import MemoryStore
from app.events import DomainEvent, EventStore


def test_event_store_appends_idempotently_and_replays_in_sequence():
    store = EventStore(MemoryStore())
    first = DomainEvent("evt-1", "symbol", "NIFTY", 1, "SETUP_DETECTED", {}, datetime.now(timezone.utc))
    second = DomainEvent("evt-2", "symbol", "NIFTY", 2, "SIGNAL_QUALIFIED", {"score": 80}, datetime.now(timezone.utc))

    assert store.append(first) is True
    assert store.append(first) is False
    assert store.append(second) is True
    replayed = store.replay("symbol", "NIFTY")
    assert [item["sequence"] for item in replayed] == [1, 2]
    assert replayed[1]["payload"] == {"score": 80}


def test_event_store_rejects_duplicate_aggregate_sequence():
    persistence = MemoryStore()
    store = EventStore(persistence)
    one = DomainEvent("evt-1", "symbol", "NIFTY", 1, "SETUP", {}, datetime.now(timezone.utc))
    two = DomainEvent("evt-2", "symbol", "NIFTY", 1, "CONFIRM", {}, datetime.now(timezone.utc))
    assert store.append(one)
    assert not store.append(two)


def test_event_store_rejects_sequence_gaps():
    store = EventStore(MemoryStore())
    gap = DomainEvent("evt-gap", "symbol", "NIFTY", 2, "CONFIRM", {}, datetime.now(timezone.utc))
    assert not store.append(gap)
    first = DomainEvent("evt-first", "symbol", "NIFTY", 1, "SETUP", {}, datetime.now(timezone.utc))
    assert store.append(first)
    assert store.replay("symbol", "NIFTY")[0]["sequence"] == 1


def test_snapshots_are_monotonic_and_restorable():
    store = EventStore(MemoryStore())
    assert store.checkpoint("symbol", "NIFTY", 4, {"phase": "ARMED"})
    assert not store.checkpoint("symbol", "NIFTY", 3, {"phase": "IDLE"})
    assert store.restore("symbol", "NIFTY")["state"] == {"phase": "ARMED"}


def test_event_model_requires_timezone_aware_time():
    with pytest.raises(ValueError):
        DomainEvent("evt", "symbol", "NIFTY", 1, "SETUP", {}, datetime(2026, 1, 1))
