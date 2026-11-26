import threading
import time

import pytest

from app.actors import ActorQueueFull, ActorRuntime


def test_actor_runtime_preserves_order_per_symbol_and_parallelizes_symbols():
    seen = []
    lock = threading.Lock()

    def handler(symbol, event):
        time.sleep(0.005)
        with lock:
            seen.append((symbol, event))
        return event * 2

    runtime = ActorRuntime(handler, max_workers=2, max_pending=20)
    try:
        futures = [runtime.submit("NIFTY", n) for n in range(5)]
        other = runtime.submit("BANKNIFTY", 99)
        assert [f.result(timeout=2) for f in futures] == [0, 2, 4, 6, 8]
        assert other.result(timeout=2) == 198
        nifty = [event for symbol, event in seen if symbol == "NIFTY"]
        assert nifty == [0, 1, 2, 3, 4]
    finally:
        runtime.close()


def test_actor_runtime_applies_global_backpressure():
    gate = threading.Event()
    started = threading.Event()

    def handler(symbol, event):
        started.set()
        gate.wait(2)
        return event

    runtime = ActorRuntime(handler, max_workers=1, max_pending=1)
    try:
        first = runtime.submit("NIFTY", 1)
        assert started.wait(1)
        with pytest.raises(ActorQueueFull):
            runtime.submit("BANKNIFTY", 2)
        gate.set()
        assert first.result(timeout=2) == 1
    finally:
        gate.set()
        runtime.close()


def test_actor_runtime_rejects_invalid_lifecycle():
    runtime = ActorRuntime(lambda symbol, event: event, max_workers=1)
    runtime.close()
    with pytest.raises(RuntimeError):
        runtime.submit("NIFTY", 1)
