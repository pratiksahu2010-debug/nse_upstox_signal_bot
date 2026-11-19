from datetime import datetime, timedelta

from app.config import IST
from app import scheduler


class FakeClient:
    def __init__(self, auth_failed=False):
        self.auth_failed = auth_failed


class FakeEngine:
    def __init__(self, auth_failed=False):
        self.client = FakeClient(auth_failed)
        self.auth_alerted_at = None
        self.messages = []

    def notify(self, message, silent=False):
        self.messages.append((message, silent))
        return True


def test_invalid_upstox_token_alert_is_throttled_and_recovers(monkeypatch):
    monkeypatch.setattr(scheduler, "UPSTOX_TOKEN", "configured-token")
    engine = FakeEngine(auth_failed=True)
    now = datetime(2026, 11, 19, 10, 0, tzinfo=IST)

    scheduler._notify_auth_state(engine, now)
    scheduler._notify_auth_state(engine, now + timedelta(minutes=4))
    assert len(engine.messages) == 1

    scheduler._notify_auth_state(engine, now + timedelta(minutes=5))
    assert len(engine.messages) == 2
    assert "rejected" in engine.messages[0][0]

    engine.client.auth_failed = False
    scheduler._notify_auth_state(engine, now + timedelta(minutes=6))
    assert engine.auth_alerted_at is None


def test_missing_upstox_token_sends_configuration_alert(monkeypatch):
    monkeypatch.setattr(scheduler, "UPSTOX_TOKEN", "")
    engine = FakeEngine(auth_failed=False)
    now = datetime(2026, 11, 19, 10, 0, tzinfo=IST)

    scheduler._notify_auth_state(engine, now)

    assert len(engine.messages) == 1
    assert "missing" in engine.messages[0][0]
