from datetime import datetime

from app import scheduler
from app.config import IST


def test_pr66_option_quote_warning_smoke(monkeypatch):
    monkeypatch.setattr(scheduler, "OPTION_LIQUIDITY_CHECKS", True)

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
    scheduler._notify_option_quote_access(
        engine, datetime(2026, 11, 23, 10, 0, tzinfo=IST)
    )
    assert len(engine.messages) == 1
    assert "CALL/PUT alerts will be suppressed" in engine.messages[0]
