from app.market_stream import MAX_INSTRUMENT_KEYS_PER_SOCKET, UpstoxMarketStream


def test_market_feed_batches_150_symbols_into_two_valid_sockets():
    keys = [f"NSE_EQ|TEST{i}" for i in range(150)]

    batches = UpstoxMarketStream._key_batches(keys)

    assert [len(batch) for batch in batches] == [100, 50]
    assert [key for batch in batches for key in batch] == keys
    assert all(len(batch) <= MAX_INSTRUMENT_KEYS_PER_SOCKET for batch in batches)


def test_market_feed_batching_handles_exact_limit_and_empty_input():
    assert [len(batch) for batch in UpstoxMarketStream._key_batches(["A"] * 100)] == [100]
    assert UpstoxMarketStream._key_batches([]) == []


def test_market_feed_batching_rejects_invalid_batch_size():
    try:
        UpstoxMarketStream._key_batches(["A"], batch_size=0)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("zero batch size must be rejected")
