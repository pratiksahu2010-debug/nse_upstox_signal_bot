from app.options import initial_option_tracking, update_option_tracking


def _quote(bid, ask, ltp, age=1, timestamp="1000"):
    return {
        "bid": bid, "ask": ask, "ltp": ltp,
        "quote_age_sec": age, "quote_timestamp": timestamp,
    }


def test_option_tracking_uses_ask_entry_and_bid_exit():
    state = initial_option_tracking(
        "NSE_FO|ABC", _quote(9.8, 10.0, 9.9), 20,
        fee_pct=0.5, slippage_bps_per_side=5,
    )
    assert state["entry_price"] == 10.0
    assert state["entry_source"] == "ASK"
    updated = update_option_tracking(state, _quote(11.9, 12.1, 12.0, timestamp="2000"), 20)
    assert round(updated["mfe_pct"], 2) == 19.0
    assert round(updated["estimated_total_cost_pct"], 2) == 0.6
    assert round(updated["estimated_net_mfe_pct"], 2) == 18.4
    assert updated["mark_source"] == "BID"


def test_option_tracking_uses_ltp_only_as_explicit_fallback():
    state = initial_option_tracking(
        "NSE_FO|ABC", _quote(None, None, 10.0), 20,
    )
    assert state["entry_price"] == 10.0
    assert state["entry_source"] == "LTP"
    updated = update_option_tracking(state, _quote(None, None, 8.0, timestamp="2000"), 20)
    assert updated["mark_source"] == "LTP"
    assert updated["mae_pct"] == -20.0


def test_option_tracking_does_not_backfill_missing_or_stale_entry():
    missing = initial_option_tracking(
        "NSE_FO|ABC", _quote(9.8, 10.0, 9.9, age=60), 20,
    )
    assert missing["entry_price"] is None
    assert missing["tracking_status"] == "MISSING_ENTRY_QUOTE"
    updated = update_option_tracking(missing, _quote(12.0, 12.2, 12.1, timestamp="2000"), 20)
    assert updated["entry_price"] is None
    assert updated["mfe_pct"] is None


def test_option_tracking_ignores_stale_and_duplicate_quotes():
    state = initial_option_tracking("NSE_FO|ABC", _quote(9.8, 10.0, 9.9), 20)
    stale = update_option_tracking(state, _quote(15.0, 15.2, 15.1, age=90, timestamp="2000"), 20)
    assert stale == state
    duplicate = update_option_tracking(state, _quote(15.0, 15.2, 15.1, timestamp="1000"), 20)
    assert duplicate == state


def test_option_tracking_updates_adverse_excursion():
    state = initial_option_tracking("NSE_FO|ABC", _quote(9.8, 10.0, 9.9), 20)
    updated = update_option_tracking(state, _quote(7.9, 8.1, 8.0, timestamp="2000"), 20)
    assert round(updated["mae_pct"], 2) == -21.0
    assert updated["mfe_pct"] == 0.0



def test_snapshot_metrics_parses_iso_timestamp_for_freshness():
    from datetime import datetime, timezone

    from app.options import snapshot_metrics

    stamp = "2026-11-19T10:00:00+05:30"
    now = datetime.fromisoformat(stamp).timestamp() + 3
    metrics = snapshot_metrics({
        "last_price": 10.0,
        "timestamp": stamp,
        "depth": {"buy": [{"price": 9.9}], "sell": [{"price": 10.1}]},
    }, now)
    assert metrics["quote_age_sec"] == 3.0
