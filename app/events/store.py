"""Small adapter over the existing persistence interface."""
from __future__ import annotations

from .model import DomainEvent


class EventStore:
    def __init__(self, persistence):
        required = ("append_domain_event", "load_domain_events",
                    "save_actor_snapshot", "load_actor_snapshot")
        missing = [name for name in required if not callable(getattr(persistence, name, None))]
        if missing:
            raise TypeError("persistence does not support event sourcing: " + ", ".join(missing))
        self.persistence = persistence

    def append(self, event: DomainEvent) -> bool:
        if not isinstance(event, DomainEvent):
            raise TypeError("event must be a DomainEvent")
        return self.persistence.append_domain_event(event.to_record())

    def replay(self, aggregate_type: str, aggregate_id: str,
               after_sequence: int = 0, limit: int = 1000) -> list[dict]:
        return self.persistence.load_domain_events(
            aggregate_type, aggregate_id, after_sequence, limit
        )

    def checkpoint(self, aggregate_type: str, aggregate_id: str,
                   version: int, state: dict) -> bool:
        if int(version) < 0:
            raise ValueError("snapshot version cannot be negative")
        if not isinstance(state, dict):
            raise TypeError("snapshot state must be a dictionary")
        return self.persistence.save_actor_snapshot(
            aggregate_type, aggregate_id, int(version), state
        )

    def restore(self, aggregate_type: str, aggregate_id: str):
        return self.persistence.load_actor_snapshot(aggregate_type, aggregate_id)
