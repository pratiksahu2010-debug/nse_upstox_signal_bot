"""Option market snapshot normalization and configurable liquidity validation.

This module does not alter the underlying CALL/PUT strategy. Production defaults
enable the hard liquidity gate; missing, stale or illiquid contracts are rejected.
The gate can be explicitly disabled with OPTION_LIQUIDITY_CHECKS=0.
"""
import math
import time
from datetime import datetime, timezone


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
    bid_qty = _finite_number(best_bid.get("quantity"))
    ask_qty = _finite_number(best_ask.get("quantity"))
    ltp = _finite_number(raw.get("last_price"))
    volume = _finite_number(raw.get("volume"))
    oi = _finite_number(raw.get("oi"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None and bid + ask > 0 else None
    spread_pct = ((ask - bid) / mid * 100) if mid and ask is not None and bid is not None else None

    timestamp = raw.get("timestamp") or raw.get("last_trade_time")
    quote_age_sec = None
    try:
        try:
            ts = float(timestamp)
            if ts > 1e11:
                ts /= 1000.0
        except (TypeError, ValueError):
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            ts = parsed.timestamp()
        age = (now_epoch if now_epoch is not None else time.time()) - ts
        # A future exchange timestamp is not proof of freshness. Treat clock-skewed
        # quotes as unusable so liquidity checks and premium tracking fail closed.
        quote_age_sec = age if age >= 0 else None
    except (TypeError, ValueError, OverflowError):
        pass

    return {
        "ltp": ltp,
        "bid": bid,
        "ask": ask,
        "bid_qty": int(bid_qty) if bid_qty is not None and bid_qty >= 0 else None,
        "ask_qty": int(ask_qty) if ask_qty is not None and ask_qty >= 0 else None,
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
    bid_qty, ask_qty = metrics.get("bid_qty"), metrics.get("ask_qty")
    if bid_qty is None or ask_qty is None or bid_qty <= 0 or ask_qty <= 0:
        return False, "depth_quantity_low"
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
            "buy": [{"price": quote.get("bid"), "quantity": quote.get("bid_qty")}],
            "sell": [{"price": quote.get("ask"), "quantity": quote.get("ask_qty")}],
        },
    }



def _timestamp_epoch(value):
    """Normalize exchange/REST quote timestamps for ordering comparisons."""
    if value is None:
        return None
    try:
        ts = float(value)
        return ts / 1000.0 if ts > 1e11 else ts
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None


def _positive(value):
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def initial_option_tracking(instrument_key: str, metrics: dict, max_quote_age_sec: int,
                            fee_pct: float = 0.0, slippage_bps_per_side: float = 0.0) -> dict:
    """Freeze a conservative option entry reference from a fresh signal-time quote.

    Long option entries use the ask; LTP is a clearly tagged fallback. If no fresh
    quote exists, the entry remains missing rather than being backfilled later.
    """
    metrics = metrics if isinstance(metrics, dict) else {}
    age = _finite_number(metrics.get("quote_age_sec"))
    fresh = age is not None and age <= max_quote_age_sec
    bid, ask, ltp = (_positive(metrics.get(k)) for k in ("bid", "ask", "ltp"))
    entry_price = ask if fresh and ask is not None else ltp if fresh else None
    entry_source = "ASK" if entry_price is not None and ask is not None else "LTP" if entry_price is not None else "MISSING"
    fee = max(0.0, _finite_number(fee_pct) or 0.0)
    slip = max(0.0, _finite_number(slippage_bps_per_side) or 0.0) * 2.0 / 100.0
    return {
        "instrument_key": instrument_key,
        "entry_price": entry_price,
        "entry_source": entry_source,
        "entry_bid": bid if fresh else None,
        "entry_ask": ask if fresh else None,
        "entry_ltp": ltp if fresh else None,
        "entry_quote_timestamp": metrics.get("quote_timestamp") if fresh else None,
        "last_bid": bid if fresh else None,
        "last_ask": ask if fresh else None,
        "last_ltp": ltp if fresh else None,
        "last_quote_timestamp": metrics.get("quote_timestamp") if fresh else None,
        "last_quote_age_sec": age if fresh else None,
        "mark_source": "BID" if bid is not None else "LTP" if ltp is not None else None,
        "mfe_points": 0.0 if entry_price is not None else None,
        "mae_points": 0.0 if entry_price is not None else None,
        "mfe_pct": 0.0 if entry_price is not None else None,
        "mae_pct": 0.0 if entry_price is not None else None,
        "estimated_fee_pct": fee,
        "estimated_slippage_pct": slip,
        "estimated_total_cost_pct": fee + slip,
        "estimated_net_mfe_pct": 0.0 if entry_price is not None else None,
        "estimated_net_mae_pct": 0.0 if entry_price is not None else None,
        "quote_samples": 1 if fresh else 0,
        "tracking_status": "TRACKING" if entry_price is not None else "MISSING_ENTRY_QUOTE",
    }


def update_option_tracking(state: dict, metrics: dict, max_quote_age_sec: int) -> dict:
    """Update quote-based premium MFE/MAE using executable sell-side bid marks."""
    out = dict(state or {})
    metrics = metrics if isinstance(metrics, dict) else {}
    age = _finite_number(metrics.get("quote_age_sec"))
    if age is None or age > max_quote_age_sec:
        return out
    entry = _positive(out.get("entry_price"))
    if entry is None:
        out["tracking_status"] = "MISSING_ENTRY_QUOTE"
        return out
    timestamp = metrics.get("quote_timestamp")
    previous_timestamp = out.get("last_quote_timestamp")
    if timestamp is not None and str(timestamp) == str(previous_timestamp):
        return out
    current_epoch = _timestamp_epoch(timestamp)
    previous_epoch = _timestamp_epoch(previous_timestamp)
    if current_epoch is not None and previous_epoch is not None and current_epoch < previous_epoch:
        # WebSocket delivery can be delayed or reordered. Never roll the latest
        # mark/sample state backwards to an older exchange quote.
        return out
    bid, ask, ltp = (_positive(metrics.get(k)) for k in ("bid", "ask", "ltp"))
    mark = bid if bid is not None else ltp
    if mark is None:
        return out
    source = "BID" if bid is not None else "LTP"
    points = mark - entry
    pct = points / entry * 100.0
    out["mfe_points"] = max(float(out.get("mfe_points") or 0.0), points)
    out["mae_points"] = min(float(out.get("mae_points") or 0.0), points)
    out["mfe_pct"] = max(float(out.get("mfe_pct") or 0.0), pct)
    out["mae_pct"] = min(float(out.get("mae_pct") or 0.0), pct)
    total_cost = float(out.get("estimated_total_cost_pct") or 0.0)
    out["estimated_net_mfe_pct"] = float(out["mfe_pct"]) - total_cost
    out["estimated_net_mae_pct"] = float(out["mae_pct"]) - total_cost
    out.update({
        "last_bid": bid, "last_ask": ask, "last_ltp": ltp,
        "last_quote_timestamp": timestamp, "last_quote_age_sec": age,
        "mark_source": source, "quote_samples": int(out.get("quote_samples") or 0) + 1,
        "tracking_status": "TRACKING",
    })
    return out
