"""Option market snapshot normalization and optional liquidity validation.

This module does not alter the underlying CALL/PUT strategy. The hard liquidity
gate is disabled by default and can only be enabled explicitly after quote access
has been verified for the configured Upstox Analytics Token.
"""
import math
import time


def _finite_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def snapshot_metrics(raw: dict, now_epoch: float = None) -> dict:
    """Normalize one Upstox full-market-quote response into stable numeric fields."""
    raw = raw if isinstance(raw, dict) else {}
    depth = raw.get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    best_bid = buys[0] if buys and isinstance(buys[0], dict) else {}
    best_ask = sells[0] if sells and isinstance(sells[0], dict) else {}

    bid = _finite_number(best_bid.get("price"))
    ask = _finite_number(best_ask.get("price"))
    ltp = _finite_number(raw.get("last_price"))
    volume = _finite_number(raw.get("volume"))
    oi = _finite_number(raw.get("oi"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None and bid + ask > 0 else None
    spread_pct = ((ask - bid) / mid * 100) if mid and ask is not None and bid is not None else None

    timestamp = raw.get("timestamp") or raw.get("last_trade_time")
    quote_age_sec = None
    try:
        ts = float(timestamp)
        if ts > 1e11:
            ts /= 1000.0
        quote_age_sec = max(0.0, (now_epoch if now_epoch is not None else time.time()) - ts)
    except (TypeError, ValueError):
        pass

    return {
        "ltp": ltp,
        "bid": bid,
        "ask": ask,
        "volume": int(volume) if volume is not None and volume >= 0 else None,
        "oi": int(oi) if oi is not None and oi >= 0 else None,
        "spread_pct": round(spread_pct, 3) if spread_pct is not None else None,
        "quote_age_sec": round(quote_age_sec, 2) if quote_age_sec is not None else None,
        "quote_timestamp": timestamp,
    }


def validate_option_liquidity(
    metrics: dict,
    min_volume: int,
    min_oi: int,
    max_spread_pct: float,
    max_quote_age_sec: int,
):
    """Return (valid, reason); missing/stale quote data fails closed when enabled."""
    bid, ask = metrics.get("bid"), metrics.get("ask")
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return False, "missing_depth"
    if ask < bid:
        return False, "crossed_book"
    spread = metrics.get("spread_pct")
    if spread is None or spread > max_spread_pct:
        return False, "spread_too_wide"
    volume = metrics.get("volume")
    if volume is None or volume < min_volume:
        return False, "volume_low"
    oi = metrics.get("oi")
    if oi is None or oi < min_oi:
        return False, "oi_low"
    age = metrics.get("quote_age_sec")
    if age is None or age > max_quote_age_sec:
        return False, "quote_stale"
    return True, "ok"


def stream_quote_snapshot(quote: dict):
    """Adapt a decoded Upstox V3 full-feed quote to the common quote normalizer."""
    if not isinstance(quote, dict):
        return None
    return {
        "last_price": quote.get("ltp"),
        "volume": quote.get("volume"),
        "oi": quote.get("oi"),
        "timestamp": quote.get("exchange_ts_ms"),
        "depth": {
            "buy": [{"price": quote.get("bid")}],
            "sell": [{"price": quote.get("ask")}],
        },
    }
