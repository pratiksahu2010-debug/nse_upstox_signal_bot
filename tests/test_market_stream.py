from app import MarketDataFeedV3_pb2 as pb
from app.market_stream import UpstoxMarketStream


class FakeClient:
    pass


def test_market_feed_decodes_live_quote_and_depth_without_touching_strategy():
    stream = UpstoxMarketStream(FakeClient())
    response = pb.FeedResponse()
    feed = response.feeds["NSE_EQ|TEST"]
    market = feed.fullFeed.marketFF
    market.ltpc.ltp = 123.45
    market.ltpc.ltt = 1795000000000
    market.vtt = 25000
    market.oi = 1000
    depth = market.marketLevel.bidAskQuote.add()
    depth.bidP = 123.40
    depth.askP = 123.50
    depth.bidQ = 10
    depth.askQ = 12

    stream._consume(response.SerializeToString())

    quote = stream.quote("NSE_EQ|TEST")
    assert quote["ltp"] == 123.45
    assert quote["volume"] == 25000
    assert quote["oi"] == 1000
    assert quote["bid"] == 123.40
    assert quote["ask"] == 123.50
    assert stream.diagnostics()["ticks"] == 1



def test_market_feed_does_not_replace_newer_quote_with_out_of_order_tick():
    stream = UpstoxMarketStream(FakeClient())

    newer = pb.FeedResponse()
    market = newer.feeds["NSE_EQ|TEST"].fullFeed.marketFF
    market.ltpc.ltp = 125.0
    market.ltpc.ltt = 1795000002000
    market.vtt = 26000
    market.oi = 1100
    depth = market.marketLevel.bidAskQuote.add()
    depth.bidP = 124.9
    depth.askP = 125.1
    stream._consume(newer.SerializeToString())

    older = pb.FeedResponse()
    market = older.feeds["NSE_EQ|TEST"].fullFeed.marketFF
    market.ltpc.ltp = 123.0
    market.ltpc.ltt = 1795000001000
    market.vtt = 25000
    market.oi = 1000
    depth = market.marketLevel.bidAskQuote.add()
    depth.bidP = 122.9
    depth.askP = 123.1
    stream._consume(older.SerializeToString())

    quote = stream.quote("NSE_EQ|TEST")
    assert quote["ltp"] == 125.0
    assert quote["exchange_ts_ms"] == 1795000002000
    assert quote["volume"] == 26000
    assert quote["bid"] == 124.9
    assert quote["ask"] == 125.1
    assert stream.diagnostics()["ticks"] == 1


def test_market_feed_does_not_replace_timestamped_quote_with_unknown_timestamp():
    stream = UpstoxMarketStream(FakeClient())

    known = pb.FeedResponse()
    market = known.feeds["NSE_EQ|TEST"].fullFeed.marketFF
    market.ltpc.ltp = 125.0
    market.ltpc.ltt = 1795000002000
    market.vtt = 26000
    market.oi = 1100
    depth = market.marketLevel.bidAskQuote.add()
    depth.bidP = 124.9
    depth.askP = 125.1
    stream._consume(known.SerializeToString())

    unknown = pb.FeedResponse()
    market = unknown.feeds["NSE_EQ|TEST"].fullFeed.marketFF
    market.ltpc.ltp = 99.0
    # Protobuf's default zero represents an absent/unknown exchange timestamp.
    market.vtt = 1
    market.oi = 1
    depth = market.marketLevel.bidAskQuote.add()
    depth.bidP = 98.9
    depth.askP = 99.1
    stream._consume(unknown.SerializeToString())

    quote = stream.quote("NSE_EQ|TEST")
    assert quote["ltp"] == 125.0
    assert quote["exchange_ts_ms"] == 1795000002000
    assert quote["volume"] == 26000
    assert quote["bid"] == 124.9
    assert quote["ask"] == 125.1



def test_market_feed_diagnostics_exposes_worker_and_message_freshness():
    stream = UpstoxMarketStream(FakeClient())
    response = pb.FeedResponse()
    feed = response.feeds["NSE_EQ|TEST"].ltpc
    feed.ltp = 123.45
    feed.ltt = 1795000000000
    stream._consume(response.SerializeToString())

    health = stream.diagnostics()

    assert health["thread_alive"] is False
    assert health["connected"] is True
    assert health["last_message_age_sec"] is not None
    assert health["last_message_age_sec"] < 5
    assert health["last_quote_age_sec"] is not None
    assert health["last_quote_age_sec"] < 5


def test_market_feed_start_replaces_dead_worker(monkeypatch):
    class DeadThread:
        def is_alive(self):
            return False

    class StartedThread:
        def __init__(self, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            self.started = False

        def is_alive(self):
            return self.started

        def start(self):
            self.started = True

    monkeypatch.setattr("app.market_stream.threading.Thread", StartedThread)
    stream = UpstoxMarketStream(FakeClient())
    stream._thread = DeadThread()

    stream.start(["NSE_EQ|TEST"])

    assert isinstance(stream._thread, StartedThread)
    assert stream._thread.started is True
    assert stream._stop.is_set() is False



def test_market_feed_subscription_change_invalidates_previous_quote_freshness():
    stream = UpstoxMarketStream(FakeClient())
    response = pb.FeedResponse()
    feed = response.feeds["NSE_EQ|OLD"].ltpc
    feed.ltp = 123.45
    feed.ltt = 1795000000000
    stream._consume(response.SerializeToString())
    assert stream.diagnostics()["last_quote_age_sec"] is not None

    stream.set_keys(["NSE_EQ|NEW"])

    assert stream.diagnostics()["last_quote_age_sec"] is None



def test_market_feed_duplicate_timestamp_does_not_refresh_quote_freshness():
    stream = UpstoxMarketStream(FakeClient())
    first = pb.FeedResponse()
    feed = first.feeds["NSE_EQ|TEST"].ltpc
    feed.ltp = 123.45
    feed.ltt = 1795000000000
    stream._consume(first.SerializeToString())
    first_seen = stream.last_quote_at

    duplicate = pb.FeedResponse()
    feed = duplicate.feeds["NSE_EQ|TEST"].ltpc
    feed.ltp = 124.00
    feed.ltt = 1795000000000
    stream._consume(duplicate.SerializeToString())

    assert stream.quote("NSE_EQ|TEST")["ltp"] == 123.45
    assert stream.last_quote_at == first_seen
    assert stream.diagnostics()["ticks"] == 1


def test_market_feed_rejects_empty_or_invalid_quotes_for_freshness():
    stream = UpstoxMarketStream(FakeClient())
    response = pb.FeedResponse()
    zero_price = response.feeds["NSE_EQ|ZERO"].ltpc
    zero_price.ltp = 0.0
    zero_price.ltt = 1795000000000
    missing_time = response.feeds["NSE_EQ|NO_TIME"].ltpc
    missing_time.ltp = 123.45
    # ltt intentionally left at protobuf's zero default.

    stream._consume(response.SerializeToString())

    health = stream.diagnostics()
    assert health["ticks"] == 0
    assert health["last_quote_age_sec"] is None
    assert stream.quote("NSE_EQ|ZERO") is None
    assert stream.quote("NSE_EQ|NO_TIME") is None



def test_market_feed_reconnect_backoff_wakes_promptly_on_stop():
    import asyncio

    stream = UpstoxMarketStream(FakeClient())

    async def scenario():
        task = asyncio.create_task(stream._wait_or_stop(30.0))
        asyncio.get_running_loop().call_later(0.05, stream._stop.set)
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(scenario())
    assert stream._stop.is_set()
