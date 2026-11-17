import app.telegram as telegram


class Response:
    def __init__(self, status, body=None, error=None):
        self.status_code = status
        self._body = body
        self._error = error
        self.text = "rate limited"

    def json(self):
        if self._error:
            raise self._error
        return self._body


def _configure(monkeypatch, responses, sleeps):
    monkeypatch.setattr(telegram, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "TELEGRAM_TOKEN", "test-token")
    monkeypatch.setattr(telegram, "TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setattr(telegram, "_last", [0.0])
    monkeypatch.setattr(telegram.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(telegram.time, "sleep", sleeps.append)
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(telegram.requests, "post", post)
    return calls


def test_telegram_429_malformed_json_retries_with_fallback(monkeypatch):
    sleeps = []
    calls = _configure(
        monkeypatch,
        [Response(429, error=ValueError("bad json")), Response(200)],
        sleeps,
    )
    assert telegram.send("alert") is True
    assert len(calls) == 2
    assert sleeps == [3.5]


def test_telegram_429_retry_after_is_bounded(monkeypatch):
    sleeps = []
    calls = _configure(
        monkeypatch,
        [Response(429, {"parameters": {"retry_after": 9999}}), Response(200)],
        sleeps,
    )
    assert telegram.send("alert") is True
    assert len(calls) == 2
    assert sleeps == [30.5]


def test_telegram_429_invalid_retry_after_uses_fallback(monkeypatch):
    sleeps = []
    calls = _configure(
        monkeypatch,
        [Response(429, {"parameters": {"retry_after": "not-a-number"}}), Response(200)],
        sleeps,
    )
    assert telegram.send("alert") is True
    assert len(calls) == 2
    assert sleeps == [3.5]
