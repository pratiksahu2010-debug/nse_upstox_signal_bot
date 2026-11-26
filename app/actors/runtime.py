"""Bounded virtual actors: one ordered mailbox per symbol on a shared worker pool.

The mailbox is process-local and deliberately not the source of truth. Events must
be durably appended before being submitted; pending work is recovered by replay
after a process restart.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("actors.runtime")


class ActorQueueFull(RuntimeError):
    """Raised when the bounded actor mailbox capacity has been reached."""


@dataclass
class _WorkItem:
    event: Any
    future: Future


class ActorRuntime:
    """Serialize work per symbol while processing different symbols concurrently."""

    def __init__(self, handler: Callable[[str, Any], Any], max_workers: int = 8,
                 max_pending: int = 1000):
        if not callable(handler):
            raise TypeError("handler must be callable")
        if int(max_workers) < 1 or int(max_pending) < 1:
            raise ValueError("max_workers and max_pending must be positive")
        self.handler = handler
        self.max_pending = int(max_pending)
        self._executor = ThreadPoolExecutor(
            max_workers=int(max_workers), thread_name_prefix="symbol-actor"
        )
        self._lock = threading.Lock()
        self._mailboxes: dict[str, deque[_WorkItem]] = {}
        self._running: set[str] = set()
        self._pending = 0
        self._closed = False

    @property
    def pending_count(self) -> int:
        with self._lock:
            return self._pending

    @property
    def active_symbols(self) -> int:
        with self._lock:
            return len(self._running)

    def submit(self, symbol: str, event: Any) -> Future:
        key = str(symbol or "").strip()
        if not key:
            raise ValueError("symbol is required")
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("actor runtime is closed")
            if self._pending >= self.max_pending:
                raise ActorQueueFull("actor mailbox capacity reached")
            mailbox = self._mailboxes.setdefault(key, deque())
            mailbox.append(_WorkItem(event, future))
            self._pending += 1
            if key not in self._running:
                self._running.add(key)
                try:
                    self._executor.submit(self._drain, key)
                except Exception:
                    self._running.discard(key)
                    self._pending -= len(mailbox)
                    self._mailboxes.pop(key, None)
                    raise
        return future

    def _drain(self, symbol: str) -> None:
        while True:
            with self._lock:
                mailbox = self._mailboxes.get(symbol)
                if not mailbox:
                    self._mailboxes.pop(symbol, None)
                    self._running.discard(symbol)
                    return
                item = mailbox.popleft()
            try:
                if not item.future.set_running_or_notify_cancel():
                    continue
                try:
                    item.future.set_result(self.handler(symbol, item.event))
                except Exception as exc:
                    item.future.set_exception(exc)
                    log.exception("symbol actor event failed symbol=%s", symbol)
            finally:
                with self._lock:
                    self._pending -= 1

    def close(self, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)
