from app.options import snapshot_metrics, validate_option_liquidity


def test_snapshot_metrics_extracts_best_depth_and_calculates_spread():
    raw = {
        "last_price": 100.0,
        "volume": 1200,
        "oi": 8000,
        "timestamp": 1_795_000_000_000,
        "depth": {
            "buy": [{"price": 99.5, "quantity": 10}],
            "sell": [{"price": 100.5, "quantity": 12}],
        },
    }
    metrics = snapshot_metrics(raw, now_epoch=1_795_000_001)

    assert metrics["ltp"] == 100.0
    assert metrics["bid"] == 99.5
    assert metrics["ask"] == 100.5
    assert metrics["spread_pct"] == 1.0
    assert metrics["volume"] == 1200
    assert metrics["oi"] == 8000
    assert metrics["quote_age_sec"] == 1.0


def test_liquidity_validator_accepts_fresh_tight_and_active_contract():
    metrics = {
        "bid": 99.5,
        "ask": 100.5,
        "spread_pct": 1.0,
        "volume": 1200,
        "oi": 8000,
        "quote_age_sec": 1.0,
    }
    assert validate_option_liquidity(metrics, 100, 500, 3.0, 20) == (True, "ok")


def test_liquidity_validator_fails_closed_for_missing_or_stale_data():
    missing = {"bid": None, "ask": None}
    assert validate_option_liquidity(missing, 100, 500, 3.0, 20) == (False, "missing_depth")

    stale = {
        "bid": 99.5,
        "ask": 100.5,
        "spread_pct": 1.0,
        "volume": 1200,
        "oi": 8000,
        "quote_age_sec": 45.0,
    }
    assert validate_option_liquidity(stale, 100, 500, 3.0, 20) == (False, "quote_stale")


def test_liquidity_validator_rejects_wide_spread_and_low_activity():
    wide = {
        "bid": 98.0,
        "ask": 102.0,
        "spread_pct": 4.0,
        "volume": 1200,
        "oi": 8000,
        "quote_age_sec": 1.0,
    }
    assert validate_option_liquidity(wide, 100, 500, 3.0, 20) == (False, "spread_too_wide")

    thin = {
        "bid": 99.5,
        "ask": 100.5,
        "spread_pct": 1.0,
        "volume": 20,
        "oi": 200,
        "quote_age_sec": 1.0,
    }
    assert validate_option_liquidity(thin, 100, 500, 3.0, 20) == (False, "volume_low")
