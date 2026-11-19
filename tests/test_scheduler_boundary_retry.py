from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import scheduler


def test_scheduler_retries_boundary_when_engine_is_busy(monkeypatch):
    now = datetime(2026, 11, 19, 10, 5, 10, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            return now

    class StopAfterTwoTicks:
        def __init__(self):
            self.ticks = 0

        def is_set(self):
            return self.ticks >= 2

        def wait(self, _seconds):
            self.ticks += 1
            return False

    class FakeEngine:
        def __init__(self):
            self.client = SimpleNamespace(auth_failed=False)
            self.market_stream = None
            self.calls = []

        def housekeeping(self, _now):
            pass

        def run_boundary(self, at):
            self.calls.append(at)
            return {"busy": True} if len(self.calls) == 1 else {"normal": {"scope": "full", "loaded": 1, "signals": 0}}

    monkeypatch.setattr(scheduler, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduler, "UPSTOX_TOKEN", "configured")
    engine = FakeEngine()
    stop = StopAfterTwoTicks()

    scheduler.loop(engine, stop)

    assert len(engine.calls) == 2
