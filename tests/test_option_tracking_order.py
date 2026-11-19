from app.options import initial_option_tracking, update_option_tracking


def test_option_tracking_ignores_older_exchange_quote_after_newer_rest_entry():
    state = initial_option_tracking(
        "NSE_FO|TEST",
        {
            "bid": 10.0, "ask": 10.2, "ltp": 10.1,
            "quote_age_sec": 1.0,
            "quote_timestamp": "2026-11-19T10:00:02+05:30",
        },
        max_quote_age_sec=10,
    )

    updated = update_option_tracking(
        state,
        {
            "bid": 9.0, "ask": 9.2, "ltp": 9.1,
            "quote_age_sec": 1.0,
            "quote_timestamp": 1795062601000,
        },
        max_quote_age_sec=10,
    )

    assert updated["last_bid"] == 10.0
    assert updated["last_ask"] == 10.2
    assert updated["last_quote_timestamp"] == "2026-11-19T10:00:02+05:30"
    assert updated["quote_samples"] == 1
