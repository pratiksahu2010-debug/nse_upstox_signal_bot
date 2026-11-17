"""Upstox REST client: rate-limited candle fetch + streaming instrument-master parser.

Endpoints used (Upstox API v3):
  GET /v3/historical-candle/intraday/{key}/minutes/5            -> today's candles
  GET /v3/historical-candle/{key}/minutes/5/{to_date}/{from_date} -> previous sessions
  https://assets.upstox.com/market-quote/instruments/exchange/{NSE|MCX}.json.gz
Candle row: [timestamp(ISO, +05:30), open, high, low, close, volume, open_interest].
Ordering is not guaranteed by the docs, so frames are always sorted by timestamp.
"""
import codecs
import gzip
import io
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Callable, Iterator, Optional
from urllib.parse import quote

import pandas as pd
import requests

from .config import ENGINE, IST, UPSTOX_TOKEN

log = logging.getLogger("upstox")
BASE = "https://api.upstox.com"
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/{seg}.json.gz"


class Budget:
    """Sliding-window limiter: per-second, per-minute and per-30-minute caps."""

    def __init__(self, per_sec: int, per_min: int, per_30: int):
        self.caps = ((1.0, per_sec), (60.0, per_min), (1800.0, per_30))
        self.hits: deque = deque()
        self.lock = threading.Lock()
        self.total = 0

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                while self.hits and now - self.hits[0] > 1800.0:
                    self.hits.popleft()
                wait = 0.0
                for win, cap in self.caps:
                    recent = [h for h in self.hits if now - h <= win]
                    if len(recent) >= cap:
                        wait = max(wait, win - (now - recent[0]) + 0.01)
                if wait <= 0:
                    self.hits.append(now)
                    self.total += 1
                    return
            time.sleep(min(wait, 5.0))

    def usage(self) -> dict:
        now = time.monotonic()
        with self.lock:
            return {
                "last_min": sum(1 for h in self.hits if now - h <= 60),
                "last_30min": sum(1 for h in self.hits if now - h <= 1800),
                "total": self.total,
            }


class UpstoxError(Exception):
    pass


class UpstoxClient:
    def __init__(self, token: str = UPSTOX_TOKEN, budget: Optional[Budget] = None):
        self.token = token
        self.budget = budget or Budget(ENGINE.rate_per_sec, ENGINE.rate_per_min, ENGINE.rate_per_30min)
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.auth_failed = False

    def _get(self, path: str, params: Optional[dict] = None, retries: int = 3) -> dict:
        url = BASE + path
        headers = {"Authorization": f"Bearer {self.token}"}
        last = None
        for attempt in range(retries):
            self.budget.acquire()
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=15)
            except requests.RequestException as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2 * (attempt + 1))))
                last = UpstoxError("429 rate limited")
                continue
            if r.status_code in (401, 403):
                self.auth_failed = True
                raise UpstoxError(f"auth error {r.status_code}: token invalid/expired")
            if r.status_code >= 500:
                last = UpstoxError(f"{r.status_code}")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise UpstoxError(f"{r.status_code}: {r.text[:200]}")
            self.auth_failed = False
            return r.json()
        raise UpstoxError(f"failed after {retries} tries: {last}")

    @staticmethod
    def _frame(candles: list) -> pd.DataFrame:
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
        df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume", "oi"][:len(candles[0])])
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(IST)
        df = df.set_index("ts").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna(subset=["open", "high", "low", "close"]).fillna({"volume": 0})

    def intraday(self, key: str, interval: int = 5) -> pd.DataFrame:
        d = self._get(f"/v3/historical-candle/intraday/{quote(key, safe='')}/minutes/{interval}")
        return self._frame(d.get("data", {}).get("candles", []))

    def historical(self, key: str, from_date, to_date, interval: int = 5) -> pd.DataFrame:
        path = (f"/v3/historical-candle/{quote(key, safe='')}/minutes/{interval}/"
                f"{to_date:%Y-%m-%d}/{from_date:%Y-%m-%d}")
        d = self._get(path)
        return self._frame(d.get("data", {}).get("candles", []))

    def ltp(self, keys: list[str]) -> dict:
        """Best-effort LTP lookup (used only for the option premium in the alert).
        Returns {instrument_key_or_symbol: price}; empty dict if the shape is unexpected."""
        try:
            d = self._get("/v2/market-quote/ltp", {"instrument_key": ",".join(keys)}, retries=2)
            out = {}
            for k, v in (d.get("data") or {}).items():
                px = v.get("last_price", v.get("ltp"))
                tok = v.get("instrument_token")
                if px is not None:
                    out[tok or k] = float(px)
            return out
        except Exception as e:  # premium is optional
            log.warning("ltp lookup failed: %s", e)
            return {}


# --------------------------------------------------------------------------- #
# Instrument master (streamed: the NSE file is far too big to json.load on 512 MB)
# --------------------------------------------------------------------------- #
class _Chain(io.RawIOBase):
    def __init__(self, head: bytes, raw):
        self.head, self.raw = head, raw

    def readable(self):
        return True

    def readinto(self, b):
        if self.head:
            n = min(len(b), len(self.head))
            b[:n] = self.head[:n]
            self.head = self.head[n:]
            return n
        data = self.raw.read(len(b))
        b[:len(data)] = data
        return len(data)


def iter_json_array(fobj, chunk: int = 1 << 20) -> Iterator[dict]:
    """Yield objects of a huge top-level JSON array without loading it in memory."""
    dec = json.JSONDecoder()
    inc = codecs.getincrementaldecoder("utf-8")()
    buf = ""
    while True:
        data = fobj.read(chunk)
        buf += inc.decode(data, final=not data)
        i = 0
        while True:
            while i < len(buf) and buf[i] in " \r\n\t,[":
                i += 1
            if i >= len(buf) or buf[i] == "]":
                break
            try:
                obj, j = dec.raw_decode(buf, i)
            except json.JSONDecodeError:
                break
            yield obj
            i = j
        buf = buf[i:]
        if not data:
            return


def stream_master(segment_file: str) -> Iterator[dict]:
    """segment_file: 'NSE' or 'MCX'. Yields instrument dicts."""
    r = requests.get(MASTER_URL.format(seg=segment_file), stream=True, timeout=120)
    r.raise_for_status()
    raw = r.raw
    raw.decode_content = False
    head = raw.read(2)
    buffered = io.BufferedReader(_Chain(head, raw), buffer_size=1 << 20)
    fobj = gzip.GzipFile(fileobj=buffered) if head == b"\x1f\x8b" else buffered
    yield from iter_json_array(fobj)


def expiry_to_date(v) -> Optional[datetime]:
    """Master 'expiry' is epoch milliseconds (handle seconds / ISO defensively)."""
    if v in (None, "", 0):
        return None
    try:
        x = float(v)
        if x > 1e11:
            x /= 1000.0
        return datetime.fromtimestamp(x, tz=IST)
    except (TypeError, ValueError):
        try:
            return pd.Timestamp(v).tz_localize(IST).to_pydatetime()
        except Exception:
            return None
