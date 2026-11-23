from app.options import snapshot_metrics, stream_quote_snapshot


def test_stream_quote_maps_v3_depth_and_timestamp_into_common_snapshot():
    raw = stream_quote_snapshot({
        "ltp": 101.25, "volume": 2500, "oi": 9000,
        "exchange_ts_ms": 1795000000000, "bid": 101.0, "ask": 101.5,
        "bid_qty": 40, "ask_qty": 55,
    })
    metrics = snapshot_metrics(raw, now_epoch=1795000001)
    assert metrics["ltp"] == 101.25
    assert metrics["bid"] == 101.0
    assert metrics["ask"] == 101.5
    assert metrics["bid_qty"] == 40
    assert metrics["ask_qty"] == 55
    assert metrics["volume"] == 2500
    assert metrics["oi"] == 9000
    assert metrics["quote_age_sec"] == 1.0


def test_stream_quote_without_depth_is_not_liquidity_valid():
    raw = stream_quote_snapshot({
        "ltp": 101.25, "volume": 2500, "oi": 9000,
        "exchange_ts_ms": 1795000000000, "bid": None, "ask": None,
    })
    metrics = snapshot_metrics(raw, now_epoch=1795000001)
    assert metrics["bid"] is None
    assert metrics["ask"] is None
