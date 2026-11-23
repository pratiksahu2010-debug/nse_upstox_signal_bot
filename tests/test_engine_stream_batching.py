from app.db import MemoryStore
from app.engine import Engine


class FakeStream:
    def __init__(self):
        self.calls = []

    def set_keys(self, keys):
        self.calls.append(list(keys))


def test_engine_batches_lazy_option_subscriptions_into_one_stream_update():
    engine = Engine(client=object(), store=MemoryStore())
    stream = FakeStream()
    engine.market_stream = stream
    engine._stream_keys = ["underlying-1", "option-1", "option-2"]
    engine._stream_keys_dirty = True

    engine._sync_market_stream_keys()
    engine._sync_market_stream_keys()

    assert stream.calls == [["underlying-1", "option-1", "option-2"]]
    assert engine._stream_keys_dirty is False



def test_engine_housekeeping_restarts_dead_market_feed_worker():
    from datetime import datetime, timedelta, timezone

    class DeadFeed:
        def __init__(self):
            self.starts = []

        def diagnostics(self):
            return {"thread_alive": False}

        def start(self, keys):
            self.starts.append(list(keys))

    engine = Engine(client=object(), store=MemoryStore())
    feed = DeadFeed()
    engine.market_stream = feed
    engine._stream_keys = ["underlying-1"]
    engine.ensure_universe = lambda now: None

    engine.housekeeping(datetime(2026, 11, 20, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))))

    assert feed.starts == [["underlying-1"]]


def test_run_boundary_records_successful_scan_health():
    from datetime import datetime, timedelta, timezone

    engine = Engine(client=object(), store=MemoryStore())
    engine.ensure_universe = lambda now: None
    engine.cycle = lambda module, now, force=False: {
        "scope": "full", "symbols": 150, "loaded": 150, "fresh": 150, "signals": 0
    }
    now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    result = engine.run_boundary(now, only="normal", force=True)

    assert result["normal"]["loaded"] == 150
    assert engine.last_cycle_completed_at is not None
    assert engine.last_successful_cycle_at is not None
    assert engine.last_cycle_error == ""
    assert engine.last_cycle_result["normal"]["symbols"] == 150


def test_run_boundary_records_scan_exception_for_readiness():
    from datetime import datetime, timedelta, timezone

    engine = Engine(client=object(), store=MemoryStore())
    engine.ensure_universe = lambda now: None

    def fail_cycle(module, now, force=False):
        raise RuntimeError("fixture scan failure")

    engine.cycle = fail_cycle
    now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    result = engine.run_boundary(now, only="normal", force=True)

    assert "error" in result["normal"]
    assert engine.last_cycle_completed_at is not None
    assert engine.last_cycle_error.startswith("RuntimeError:")
