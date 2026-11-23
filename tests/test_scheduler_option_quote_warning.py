from datetime import datetime, timedelta

from app import scheduler
from app.config import IST


def test_option_quote_access_warning_is_throttled(monkeypatch):
    monkeypatch.setattr(scheduler, "OPTION_LIQUIDITY_CHECKS", True)

    class Client:
        def diagnostics(self):
            return {"optional_data_disabled": ["full_quotes"]}

    class Engine:
        client = Client()

        def __init__(self):
            self.last_heartbeat = {}
            self.messages = []

        def notify(self, message):
            self.messages.append(message)

    engine = Engine()
    now = datetime(2026, 11, 23, 10, 0, tzinfo=IST)

    scheduler._notify_option_quote_access(engine, now)
    scheduler._notify_option_quote_access(engine, now + timedelta(minutes=4))
    assert len(engine.messages) == 1
    assert "strict option-liquidity gate" in engine.messages[0]

    scheduler._notify_option_quote_access(engine, now + timedelta(minutes=5))
    assert len(engine.messages) == 2


def test_option_quote_warning_is_silent_when_liquidity_gate_is_disabled(monkeypatch):
    monkeypatch.setattr(scheduler, "OPTION_LIQUIDITY_CHECKS", False)

    class Client:
        def diagnostics(self):
            return {"optional_data_disabled": ["full_quotes"]}

    class Engine:
        client = Client()
        last_heartbeat = {}

        def __init__(self):
            self.messages = []

        def notify(self, message):
            self.messages.append(message)

    engine = Engine()
    scheduler._notify_option_quote_access(engine, datetime(2026, 11, 23, 10, 0, tzinfo=IST))
    assert engine.messages == []
