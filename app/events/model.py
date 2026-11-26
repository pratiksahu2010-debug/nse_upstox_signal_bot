"""Validated immutable event envelope used by the actor/CQRS layer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class DomainEvent:
    event_id: str
    aggregate_type: str
    aggregate_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime

    @classmethod
    def create(cls, aggregate_type: str, aggregate_id: str, sequence: int,
               event_type: str, payload: dict[str, Any] | None = None,
               occurred_at: datetime | None = None):
        return cls(
            event_id=uuid4().hex,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            sequence=int(sequence),
            event_type=event_type,
            payload=dict(payload or {}),
            occurred_at=occurred_at or datetime.now(timezone.utc),
        )

    def __post_init__(self):
        if not self.event_id or not self.aggregate_type or not self.aggregate_id or not self.event_type:
            raise ValueError("event identity, aggregate and type are required")
        if int(self.sequence) < 1:
            raise ValueError("event sequence must be positive")
        if not isinstance(self.payload, dict):
            raise TypeError("event payload must be a dictionary")
        if not isinstance(self.occurred_at, datetime):
            raise TypeError("occurred_at must be a datetime")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")

    def to_record(self) -> dict:
        return {
            "event_id": self.event_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "sequence": int(self.sequence),
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "occurred_at": self.occurred_at.astimezone(timezone.utc),
        }
