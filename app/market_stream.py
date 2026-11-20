"""Upstox Market Data Feed V3 client with reconnect and resubscription.

The feed supplies live quote and provider-health observations. The strategy continues to
consume only completed 5-minute candles from the existing validated candle pipeline.
Ticks are never treated as completed candles and cannot create early signals.
"""
import asyncio
import json
import logging
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
        self.last_error = ""
        self.reconnects = 0
        self.ticks = 0

    def start(self, instrument_keys):
        self.set_keys(instrument_keys)
        if not self._keys or self._thread is not None:
            return
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

    def diagnostics(self):
        with self._lock:
            return {
                "connected": self.connected,
                "subscribed_symbols": len(self._keys),
                "ticks": self.ticks,
                "reconnects": self.reconnects,
                "last_message_at": self.last_message_at,
                "last_error": self.last_error,
                "quote_cache_size": len(self._quotes),
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
                    uri, ping_interval=20, close_timeout=5, max_size=4 * 1024 * 1024
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
                    log.warning("Upstox market feed disconnected; reconnecting: %s", type(exc).__name__)
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

            quote = {
                "ltp": float(ltpc.ltp),
                "exchange_ts_ms": int(ltpc.ltt),
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
            for key, quote in updates.items():
                previous = self._quotes.get(key)
                previous_ts = int(previous.get("exchange_ts_ms") or 0) if previous else 0
                incoming_ts = int(quote.get("exchange_ts_ms") or 0)
                # Upstox WebSocket delivery can be reordered. Do not let an
                # older exchange tick roll the cached quote backwards. A zero
                # timestamp is treated as unknown for backward compatibility.
                if previous_ts > 0 and (incoming_ts <= 0 or incoming_ts < previous_ts):
                    # An unknown timestamp cannot establish that this update is newer.
                    # Preserve the last quote whose exchange ordering is known.
                    continue
                self._quotes[key] = quote
            self.ticks += len(updates)
            self.last_message_at = now
            self.connected = True
