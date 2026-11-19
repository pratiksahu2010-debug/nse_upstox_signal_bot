from app.market_stream import UpstoxMarketStream


def test_set_keys_prunes_quotes_for_unsubscribed_instruments():
    stream = UpstoxMarketStream(client=object())
    stream.set_keys(["NSE_INDEX|Nifty 50", "NSE_FO|OLD_OPTION", "NSE_FO|LIVE_OPTION"])
    stream._quotes = {
        "NSE_INDEX|Nifty 50": {"ltp": 25000.0},
        "NSE_FO|OLD_OPTION": {"ltp": 10.0},
        "NSE_FO|LIVE_OPTION": {"ltp": 20.0},
    }

    stream.set_keys(["NSE_INDEX|Nifty 50", "NSE_FO|LIVE_OPTION", "NSE_FO|NEW_OPTION"])

    assert stream.quote("NSE_INDEX|Nifty 50") == {"ltp": 25000.0}
    assert stream.quote("NSE_FO|OLD_OPTION") is None
    assert stream.quote("NSE_FO|LIVE_OPTION") == {"ltp": 20.0}
    assert stream.quote("NSE_FO|NEW_OPTION") is None
    assert stream.diagnostics()["quote_cache_size"] == 2
