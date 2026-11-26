from datetime import datetime, timezone

import pytest

from app.actors.event_sourced import EventAppendConflict, EventSourcedActorRuntime
from app.db import MemoryStore
from app.events import EventStore


def reducer(state, record):
    state = dict(state)
    state["bars"] = list(state.get("bars", [])) + [record["payload"]["bar"]]
    state["last_bar"] = record["payload"]["bar"]
    return state


def test_event_sourced_actor_persists_before_apply_and_recovers_after_restart():
    persistence = MemoryStore()
    store = EventStore(persistence)
    first = EventSourcedActorRuntime(
        store, reducer, max_workers=1, snapshot_every=2, replay_page_size=2
    )
    first.recover(["NIFTY"])
    try:
        event1, future1 = first.publish("NIFTY", "COMPLETED_5M_CANDLE", {"bar": "09:20"})
        assert store.replay("symbol", "NIFTY")[0]["event_id"] == event1.event_id
        assert future1.result(timeout=2)["last_bar"] == "09:20"
        event2, future2 = first.publish("NIFTY", "COMPLETED_5M_CANDLE", {"bar": "09:25"})
        assert future2.result(timeout=2)["bars"] == ["09:20", "09:25"]
        assert store.restore("symbol", "NIFTY")["version"] == 2
    finally:
        first.close()

    recovered = EventSourcedActorRuntime(
        store, reducer, max_workers=1, snapshot_every=2, replay_page_size=1
    )
    try:
        assert recovered.recover(["NIFTY"]) == {"NIFTY": 2}
        assert recovered.state("NIFTY") == {
            "bars": ["09:20", "09:25"], "last_bar": "09:25"
        }
        event3, future3 = recovered.publish(
            "NIFTY", "COMPLETED_5M_CANDLE", {"bar": "09:30"},
            occurred_at=datetime.now(timezone.utc),
        )
        assert event3.sequence == 3
        assert future3.result(timeout=2)["last_bar"] == "09:30"
    finally:
        recovered.close()


def test_event_sourced_actor_requires_recovery_and_rejects_sequence_conflict():
    store = EventStore(MemoryStore())
    runtime = EventSourcedActorRuntime(store, reducer, max_workers=1)
    try:
        with pytest.raises(RuntimeError):
            runtime.publish("NIFTY", "BAR", {"bar": "09:20"})
        runtime.recover(["NIFTY"])
        event, future = runtime.publish("NIFTY", "BAR", {"bar": "09:20"})
        future.result(timeout=2)
        # A second writer using the same aggregate sequence is rejected by the store.
        from app.events import DomainEvent
        duplicate = DomainEvent(
            "external-duplicate", "symbol", "NIFTY", 2, "BAR",
            {"bar": "09:25"}, datetime.now(timezone.utc),
        )
        assert store.append(duplicate)
        with pytest.raises(EventAppendConflict):
            runtime.publish("NIFTY", "BAR", {"bar": "09:30"})
    finally:
        runtime.close()


def test_recovery_rejects_event_sequence_gaps():
    store = EventStore(MemoryStore())
    from app.events import DomainEvent
    assert store.append(DomainEvent(
        "event-1", "symbol", "NIFTY", 1, "BAR", {"bar": "09:20"},
        datetime.now(timezone.utc),
    ))
    assert store.append(DomainEvent(
        "event-2", "symbol", "NIFTY", 2, "BAR", {"bar": "09:25"},
        datetime.now(timezone.utc),
    ))
    # Corrupt a test snapshot to represent a version ahead of available events.
    store.checkpoint("symbol", "NIFTY", 2, {"bars": ["09:20", "09:25"]})
    persistence = store.persistence
    persistence.domain_events = [
        row for row in persistence.domain_events if row["sequence"] != 1
    ]
    # MemoryStore snapshot validation correctly prevents writing an ahead checkpoint;
    # delete it to exercise replay gap detection instead.
    persistence.actor_snapshots.clear()
    runtime = EventSourcedActorRuntime(store, reducer, max_workers=1)
    try:
        with pytest.raises(EventAppendConflict):
            runtime.recover(["NIFTY"])
    finally:
        runtime.close()
