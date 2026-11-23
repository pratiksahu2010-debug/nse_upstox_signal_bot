import asyncio

from app import web


def test_shutdown_joins_scheduler_before_closing_store(monkeypatch):
    events = []

    class FakeThread:
        def __init__(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            events.append(("join", timeout))
            self.alive = False

    class FakeStop:
        def __init__(self):
            self.thread = FakeThread()

        def set(self):
            events.append(("stop", None))

    class FakeStore:
        def close(self):
            events.append(("store_close", None))

    monkeypatch.setenv("AUTOSTART", "0")
    monkeypatch.setattr(web, "_startup_thread", None)
    monkeypatch.setattr(web, "_scheduler_stop", FakeStop())
    monkeypatch.setattr(web, "_telegram_poller", None)
    monkeypatch.setattr(web, "engine", None)
    monkeypatch.setattr(web, "store", FakeStore())

    async def run():
        async with web.lifespan(None):
            pass

    asyncio.run(run())

    assert events[0] == ("stop", None)
    assert events[1][0] == "join"
    assert events[2] == ("store_close", None)
