"""Durable event-sourced actor orchestration.

Events are appended before they enter the in-process mailbox. On restart, actors
restore a checkpoint and replay the remaining immutable events through a pure
reducer. Reducers must not send notifications, place orders, or call external
services; replay is state reconstruction only.
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime

from .runtime import ActorRuntime
from ..events import DomainEvent


class EventAppendConflict(RuntimeError):
    """The event store rejected a stale, duplicate, or out-of-order event."""


class EventSourcedActorRuntime:
    def __init__(self, event_store, reducer, *, aggregate_type="symbol",
                 max_workers=8, max_pending=1000, snapshot_every=20,
                 replay_page_size=500):
        if not callable(reducer):
            raise TypeError("reducer must be callable")
        if int(snapshot_every) < 1 or int(replay_page_size) < 1:
            raise ValueError("snapshot_every and replay_page_size must be positive")
        self.event_store = event_store
        self.reducer = reducer
        self.aggregate_type = str(aggregate_type)
        self.snapshot_every = int(snapshot_every)
        self.replay_page_size = int(replay_page_size)
        self._lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._aggregate_locks = {}
        self._states = {}
        self._versions = {}
        self._applied_versions = {}
        self._enqueued_versions = {}
        self._failed = {}
        self._runtime = ActorRuntime(self._apply, max_workers=max_workers,
                                     max_pending=max_pending)
        self._closed = False

    def _aggregate_lock(self, aggregate_id):
        with self._state_lock:
            return self._aggregate_locks.setdefault(aggregate_id, threading.RLock())

    def recover(self, aggregate_ids):
        """Restore each aggregate from snapshot + ordered event pages.

        Call before publishing to an aggregate. Replay invokes only the supplied
        reducer and checkpoint persistence; no delivery side effects are present.
        """
        recovered = {}
        for raw_id in aggregate_ids:
            aggregate_id = str(raw_id or "").strip()
            if not aggregate_id:
                raise ValueError("aggregate id is required")
            snapshot = self.event_store.restore(self.aggregate_type, aggregate_id)
            if snapshot:
                version = int(snapshot["version"])
                state = copy.deepcopy(snapshot["state"])
            else:
                version, state = 0, {}
            while True:
                batch = self.event_store.replay(
                    self.aggregate_type, aggregate_id, version,
                    self.replay_page_size,
                )
                if not batch:
                    break
                for record in batch:
                    sequence = int(record["sequence"])
                    if sequence != version + 1:
                        raise EventAppendConflict(
                            f"event replay gap for {aggregate_id}: "
                            f"expected {version + 1}, got {sequence}"
                        )
                    state = self.reducer(state, record)
                    if not isinstance(state, dict):
                        raise TypeError("actor reducer must return a dictionary")
                    version = sequence
                if len(batch) < self.replay_page_size:
                    break
            with self._aggregate_lock(aggregate_id):
                with self._state_lock:
                    self._states[aggregate_id] = copy.deepcopy(state)
                    self._versions[aggregate_id] = version
                    self._applied_versions[aggregate_id] = version
                    self._enqueued_versions[aggregate_id] = version
            if version:
                self.event_store.checkpoint(
                    self.aggregate_type, aggregate_id, version, state
                )
            recovered[aggregate_id] = version
        return recovered

    def publish(self, aggregate_id, event_type, payload=None,
                occurred_at: datetime | None = None):
        """Persist first, then enqueue; queue failure never loses the durable event."""
        key = str(aggregate_id or "").strip()
        if not key:
            raise ValueError("aggregate id is required")
        with self._lock:
            if self._closed:
                raise RuntimeError("event-sourced actor runtime is closed")
            if key in self._failed:
                raise RuntimeError(f"aggregate {key} actor failed; restart and replay is required")
            if key not in self._versions:
                raise RuntimeError(f"aggregate {key} must be recovered before publish")
            sequence = self._versions[key] + 1
            event = DomainEvent.create(
                self.aggregate_type, key, sequence, event_type,
                payload=payload, occurred_at=occurred_at,
            )
            if not self.event_store.append(event):
                raise EventAppendConflict(
                    f"event append rejected for {key} sequence {sequence}"
                )
            self._versions[key] = sequence
            future = self._enqueue_pending(key, sequence)
            return event, future

    def flush(self, aggregate_id):
        """Enqueue durable events not yet accepted by the local mailbox."""
        key = str(aggregate_id or "").strip()
        with self._lock:
            if self._closed:
                raise RuntimeError("event-sourced actor runtime is closed")
            if key in self._failed:
                raise RuntimeError(f"aggregate {key} actor failed; restart and replay is required")
            if key not in self._versions:
                raise RuntimeError(f"aggregate {key} must be recovered before flush")
            return self._enqueue_pending(key, self._versions[key])

    def _enqueue_pending(self, key, through_version):
        futures = []
        enqueued = self._enqueued_versions.get(key, 0)
        while enqueued < through_version:
            records = self.event_store.replay(
                self.aggregate_type, key, enqueued, self.replay_page_size
            )
            if not records:
                raise EventAppendConflict(
                    f"durable event {key}/{enqueued + 1} is missing"
                )
            for record in records:
                if int(record["sequence"]) != enqueued + 1:
                    raise EventAppendConflict(
                        f"durable event sequence gap for {key}"
                    )
                future = self._runtime.submit(key, record)
                enqueued += 1
                self._enqueued_versions[key] = enqueued
                future.add_done_callback(
                    lambda completed, symbol=key: self._record_actor_failure(symbol, completed)
                )
                futures.append(future)
                if enqueued >= through_version:
                    break
        return futures[-1] if futures else None

    def _record_actor_failure(self, aggregate_id, future):
        if future.cancelled():
            error = "actor event was cancelled"
        else:
            try:
                exception = future.exception()
            except Exception as exc:
                exception = exc
            if exception is None:
                return
            error = f"{type(exception).__name__}: {str(exception)[:200]}"
        with self._lock:
            self._failed[aggregate_id] = error
        # Fail closed for this aggregate. Its durable ledger remains intact and
        # a process restart will reconstruct it from the last valid checkpoint.

    def _apply(self, aggregate_id, record):
        key = str(aggregate_id)
        # A separate lock per aggregate preserves same-symbol ordering without
        # serializing unrelated symbols behind one global state lock.
        with self._aggregate_lock(key):
            with self._state_lock:
                applied = self._applied_versions.get(key, 0)
                current_state = copy.deepcopy(self._states.get(key, {}))
            sequence = int(record["sequence"])
            if sequence != applied + 1:
                raise EventAppendConflict(
                    f"actor sequence gap for {key}: expected {applied + 1}, got {sequence}"
                )
            next_state = self.reducer(current_state, record)
            if not isinstance(next_state, dict):
                raise TypeError("actor reducer must return a dictionary")
            with self._state_lock:
                self._states[key] = next_state
                self._applied_versions[key] = sequence
            if sequence % self.snapshot_every == 0:
                self.event_store.checkpoint(
                    self.aggregate_type, key, sequence, next_state
                )
            return copy.deepcopy(next_state)

    def state(self, aggregate_id):
        key = str(aggregate_id)
        with self._aggregate_lock(key):
            with self._state_lock:
                return copy.deepcopy(self._states.get(key, {}))

    def diagnostics(self):
        with self._lock, self._state_lock:
            return {
                "closed": self._closed,
                "aggregates": len(self._versions),
                "persisted_events": sum(self._versions.values()),
                "applied_events": sum(self._applied_versions.values()),
                "pending": self._runtime.pending_count,
                "active_symbols": self._runtime.active_symbols,
                "failed_aggregates": dict(self._failed),
            }

    def close(self, wait=True):
        with self._lock:
            self._closed = True
        self._runtime.close(wait=wait)
