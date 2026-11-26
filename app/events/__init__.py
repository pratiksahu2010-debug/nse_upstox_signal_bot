"""CQRS/event-sourcing primitives."""
from .model import DomainEvent
from .store import EventStore

__all__ = ["DomainEvent", "EventStore"]
