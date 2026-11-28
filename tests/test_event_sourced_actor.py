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
        _, future3 = first.publish("NIFTY", "COMPLETED_5M_CANDLE", {"bar": "09:30"})
        assert future3.result(timeout=2)["last_bar"] == "09:30"
    finally:
        first.close()

    recovered = EventSourcedActorRuntime(
        store, reducer, max_workers=1, snapshot_every=2, replay_page_size=1
    )
    try:
        assert recovered.recover(["NIFTY"]) == {"NIFTY": 3}
        assert recovered.state("NIFTY") == {
            "bars": ["09:20", "09:25", "09:30"], "last_bar": "09:30"
        }
        event3, future3 = recovered.publish(
            "NIFTY", "COMPLETED_5M_CANDLE", {"bar": "09:35"},
            occurred_at=datetime.now(timezone.utc),
        )
        assert event3.sequence == 4
        assert future3.result(timeout=2)["last_bar"] == "09:35"
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



def test_persisted_event_survives_mailbox_backpressure_and_flushes_later():
    import threading

    from app.actors import ActorQueueFull

    gate = threading.Event()
    started = threading.Event()

    def slow_reducer(state, record):
        started.set()
        gate.wait(2)
        return reducer(state, record)

    persistence = MemoryStore()
    store = EventStore(persistence)
    runtime = EventSourcedActorRuntime(
        store, slow_reducer, max_workers=1, max_pending=1
    )
    runtime.recover(["NIFTY"])
    try:
        first_event, first_future = runtime.publish("NIFTY", "BAR", {"bar": "09:20"})
        assert started.wait(1)
        with pytest.raises(ActorQueueFull):
            runtime.publish("NIFTY", "BAR", {"bar": "09:25"})
        assert len(store.replay("symbol", "NIFTY")) == 2
        gate.set()
        assert first_future.result(timeout=2)["last_bar"] == "09:20"
        pending_future = runtime.flush("NIFTY")
        assert pending_future.result(timeout=2)["last_bar"] == "09:25"
    finally:
        gate.set()
        runtime.close()


def test_event_sourced_actors_process_different_symbols_concurrently():
    import threading

    barrier = threading.Barrier(2, timeout=2)

    def concurrent_reducer(state, record):
        barrier.wait()
        return reducer(state, record)

    store = EventStore(MemoryStore())
    runtime = EventSourcedActorRuntime(
        store, concurrent_reducer, max_workers=2, max_pending=4
    )
    runtime.recover(["NIFTY", "BANKNIFTY"])
    try:
        _, nifty = runtime.publish("NIFTY", "BAR", {"bar": "09:20"})
        _, bank = runtime.publish("BANKNIFTY", "BAR", {"bar": "09:20"})
        assert nifty.result(timeout=3)["last_bar"] == "09:20"
        assert bank.result(timeout=3)["last_bar"] == "09:20"
    finally:
        runtime.close()



def test_failed_actor_fails_closed_until_restart_recovery():
    def broken_reducer(state, record):
        raise ValueError("reducer failure")

    store = EventStore(MemoryStore())
    runtime = EventSourcedActorRuntime(store, broken_reducer, max_workers=1)
    runtime.recover(["NIFTY"])
    try:
        _, future = runtime.publish("NIFTY", "BAR", {"bar": "09:20"})
        with pytest.raises(ValueError):
            future.result(timeout=2)
        with pytest.raises(RuntimeError, match="restart and replay"):
            runtime.publish("NIFTY", "BAR", {"bar": "09:25"})
        assert "NIFTY" in runtime.diagnostics()["failed_aggregates"]
    finally:
        runtime.close()
