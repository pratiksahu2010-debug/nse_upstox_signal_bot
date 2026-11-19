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
