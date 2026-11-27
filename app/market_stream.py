"""Upstox Market Data Feed V3 client with reconnect and resubscription.

The feed supplies live quote and provider-health observations. The strategy continues to
consume only completed 5-minute candles from the existing validated candle pipeline.
Ticks are never treated as completed candles and cannot create early signals.
"""
import asyncio
import json
import logging
import math
import threading
import uuid
from datetime import datetime, timezone

import websockets

from . import MarketDataFeedV3_pb2 as pb

log = logging.getLogger("upstox.market_stream")


class UpstoxMarketStream:
    def __init__(self, client):
        self.client = client
        self._keys = []
        self._stop = threading.Event()
        self._resubscribe = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._quotes = {}
        self.connected = False
        self.last_message_at = None
        self.last_quote_at = None
        self.last_error = ""
        self.reconnects = 0
        self.ticks = 0

    def start(self, instrument_keys):
        self.set_keys(instrument_keys)
        if not self._keys:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        # A dead worker must not permanently disable the feed. Clear a prior
        # stop flag and create a fresh worker whenever start is called again.
        self._stop.clear()
        self._resubscribe.clear()
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._run()),
            name="upstox-market-feed",
            daemon=True,
        )
        self._thread.start()

    def set_keys(self, instrument_keys):
        new_keys = sorted(set(key for key in instrument_keys if key))
        with self._lock:
            changed = new_keys != self._keys
            self._keys = new_keys
            if changed:
                # A quote for the previous subscription set must not make a
                # newly expanded/replaced subscription set look fresh.
                self.last_quote_at = None
            # The engine rotates option subscriptions during the session. Keep
            # cached quote state only for currently subscribed instruments.
            active_keys = set(new_keys)
            self._quotes = {
                key: quote for key, quote in self._quotes.items()
                if key in active_keys
            }
        if changed and self._thread is not None and self._thread.is_alive():
            self._resubscribe.set()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=4)
        with self._lock:
            self.connected = False

    def quote(self, instrument_key):
        with self._lock:
            value = self._quotes.get(instrument_key)
            return dict(value) if value is not None else None

    def wait_for_quotes(self, instrument_keys, max_age_sec=20.0,
                        timeout_sec=2.0, poll_sec=0.1):
        """Wait briefly for fresh first ticks after a lazy option subscription.

        Exchange timestamps, not local receipt time, determine freshness. A
        missing/stale quote remains missing when the bounded wait expires.
        """
        import time

        keys = list(dict.fromkeys(key for key in instrument_keys if key))
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        while True:
            now_ms = time.time() * 1000.0
            fresh = {}
            for key in keys:
                quote = self.quote(key)
                if not quote:
                    continue
                try:
                    stamp_ms = float(quote.get("exchange_ts_ms") or 0)
                    age_sec = (now_ms - stamp_ms) / 1000.0
                except (TypeError, ValueError):
                    continue
                if 0.0 <= age_sec <= max(0.0, float(max_age_sec)):
                    fresh[key] = quote
            if len(fresh) == len(keys) or time.monotonic() >= deadline:
                return fresh
            time.sleep(min(max(0.01, float(poll_sec)),
                           max(0.0, deadline - time.monotonic())))

    def diagnostics(self):
        with self._lock:
            last_message_at = self.last_message_at
            last_quote_at = self.last_quote_at
            connected = self.connected
            keys = len(self._keys)
            ticks = self.ticks
            reconnects = self.reconnects
            last_error = self.last_error
            cache_size = len(self._quotes)
        def age_seconds(value):
            if not value:
                return None
            try:
                stamp = datetime.fromisoformat(value)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                return max(0.0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

        message_age = age_seconds(last_message_at)
        quote_age = age_seconds(last_quote_at)
        return {
            "connected": connected,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "subscribed_symbols": keys,
            "ticks": ticks,
            "reconnects": reconnects,
            "last_message_at": last_message_at,
            "last_message_age_sec": round(message_age, 2) if message_age is not None else None,
            "last_quote_at": last_quote_at,
            "last_quote_age_sec": round(quote_age, 2) if quote_age is not None else None,
            "last_error": last_error,
            "quote_cache_size": cache_size,
        }

    async def _run(self):
        delay = 1.0
        while not self._stop.is_set():
            try:
                auth = self.client._get("/v3/feed/market-data-feed/authorize", retries=1)
                uri = (auth.get("data") or {}).get("authorized_redirect_uri")
                if not uri or not uri.startswith("wss://"):
                    raise RuntimeError("Upstox did not return a valid authorized WebSocket URI")
                async with websockets.connect(
                    uri,
                    extra_headers={"Accept": "*/*"},
                    ping_interval=20,
                    close_timeout=5,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    with self._lock:
                        keys = list(self._keys)
                        self.connected = True
                        self.last_error = ""
                    request = {
                        "guid": str(uuid.uuid4()),
                        "method": "sub",
                        "data": {"mode": "full", "instrumentKeys": keys},
                    }
                    await ws.send(json.dumps(request).encode("utf-8"))
                    delay = 1.0
                    while not self._stop.is_set():
                        if self._resubscribe.is_set():
                            self._resubscribe.clear()
                            await ws.close()
                            break
                        try:
                            payload = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        if isinstance(payload, str):
                            payload = payload.encode("utf-8")
                        self._consume(payload)
            except Exception as exc:
                with self._lock:
                    self.connected = False
                    self.last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
                    self.reconnects += 1
                if not self._stop.is_set():
                    # Include the provider's HTTP status/handshake reason. The
                    # authorized redirect URI and bearer token are never logged.
                    log.warning("Upstox market feed disconnected; reconnecting: %s", self.last_error)
                    await asyncio.sleep(delay)
                    delay = min(30.0, delay * 2)
        with self._lock:
            self.connected = False

    def _consume(self, payload):
        response = pb.FeedResponse()
        response.ParseFromString(payload)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        updates = {}
        for key, feed in response.feeds.items():
            kind = feed.WhichOneof("FeedUnion")
            market_body = None
            first_level_body = None
            if kind == "ltpc":
                ltpc = feed.ltpc
            elif kind == "fullFeed":
                union = feed.fullFeed.WhichOneof("FullFeedUnion")
                if union == "marketFF":
                    market_body = feed.fullFeed.marketFF
                    ltpc = market_body.ltpc
                elif union == "indexFF":
                    ltpc = feed.fullFeed.indexFF.ltpc
                else:
                    continue
            elif kind == "firstLevelWithGreeks":
                first_level_body = feed.firstLevelWithGreeks
                ltpc = first_level_body.ltpc
            else:
                continue

            ltp = float(ltpc.ltp)
            exchange_ts_ms = int(ltpc.ltt)
            # A protobuf default (0) or non-finite price is not a real quote.
            # It must not make the feed look fresh to /ready or option tracking.
            if not math.isfinite(ltp) or ltp <= 0 or exchange_ts_ms <= 0:
                continue
            quote = {
                "ltp": ltp,
                "exchange_ts_ms": exchange_ts_ms,
                "received_at": now,
                "volume": None,
                "oi": None,
                "bid": None,
                "ask": None,
                "bid_qty": None,
                "ask_qty": None,
            }
            if market_body is not None:
                quote["volume"] = int(market_body.vtt)
                quote["oi"] = float(market_body.oi)
                levels = market_body.marketLevel.bidAskQuote
                if levels:
                    quote["bid"] = float(levels[0].bidP)
                    quote["ask"] = float(levels[0].askP)
                    quote["bid_qty"] = int(levels[0].bidQ)
                    quote["ask_qty"] = int(levels[0].askQ)
            elif first_level_body is not None:
                quote["volume"] = int(first_level_body.vtt)
                quote["oi"] = float(first_level_body.oi)
                quote["bid"] = float(first_level_body.firstDepth.bidP)
                quote["ask"] = float(first_level_body.firstDepth.askP)
                quote["bid_qty"] = int(first_level_body.firstDepth.bidQ)
                quote["ask_qty"] = int(first_level_body.firstDepth.askQ)
            updates[key] = quote

        with self._lock:
            accepted = 0
            for key, quote in updates.items():
                previous = self._quotes.get(key)
                previous_ts = int(previous.get("exchange_ts_ms") or 0) if previous else 0
                incoming_ts = int(quote.get("exchange_ts_ms") or 0)
                # Upstox WebSocket delivery can be reordered or duplicated.
                # Only a strictly newer exchange timestamp refreshes quote
                # freshness; duplicate ticks must not keep a dead feed alive.
                if previous_ts > 0 and incoming_ts <= previous_ts:
                    continue
                self._quotes[key] = quote
                accepted += 1
            self.ticks += accepted
            self.last_message_at = now
            if accepted:
                self.last_quote_at = now
            self.connected = True
