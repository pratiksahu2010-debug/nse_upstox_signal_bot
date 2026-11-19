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
