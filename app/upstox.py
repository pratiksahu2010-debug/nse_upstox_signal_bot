"""Upstox REST client: rate-limited, thread-safe candle fetch and streamed instrument master.

Analytics Tokens are read-only market-data credentials valid for one year. They do
not use the daily OAuth refresh flow; a replacement token must be generated in
Upstox Developer Apps and supplied to set_token()/the Render environment.
"""
import codecs
import gzip
import io
import json
import logging
import math
import threading
import time
from collections import deque
from datetime import datetime
from typing import Iterator, Optional
from urllib.parse import quote

import pandas as pd
import requests

from .config import ENGINE, IST, UPSTOX_TOKEN

log = logging.getLogger("upstox")
BASE = "https://api.upstox.com"
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/{seg}.json.gz"
OHLCV = ["open", "high", "low", "close", "volume", "oi"]


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
        self._token = token or ""
        self.budget = budget or Budget(ENGINE.rate_per_sec, ENGINE.rate_per_min, ENGINE.rate_per_30min)
        self._local = threading.local()
        self._state_lock = threading.Lock()
        self.auth_failed = False
        self.auth_status = None
        self.last_success_at = None
        self.last_error = ""
        self.last_status = None
        self.request_count = 0
        self.failure_count = 0

    def _session(self):
        # requests.Session is not documented as thread-safe. Each worker owns one.
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"Accept": "application/json"})
            self._local.session = session
        return session

    def set_token(self, token: str):
        """Install a newly generated token and clear the latched auth failure."""
        token = (token or "").strip()
        if not token:
            raise ValueError("new Upstox token must not be empty")
        with self._state_lock:
            self._token = token
            self.auth_failed = False
            self.auth_status = None
            self.last_error = ""
            self.last_status = None

    def diagnostics(self) -> dict:
        with self._state_lock:
            return {
                "token_configured": bool(self._token),
                "auth_failed": self.auth_failed,
                "auth_status": self.auth_status,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error,
                "last_status": self.last_status,
                "request_count": self.request_count,
                "failure_count": self.failure_count,
            }

    @staticmethod
    def _retry_after(value, fallback: float) -> float:
        try:
            # Upstox normally returns seconds. Ignore HTTP-date or malformed values.
            return max(0.5, min(30.0, float(value)))
        except (TypeError, ValueError):
            return max(0.5, min(30.0, fallback))

    def _get(self, path: str, params: Optional[dict] = None, retries: int = 3,
             *, latch_auth: bool = True, api_version: Optional[str] = None) -> dict:
        """GET an Upstox endpoint. Optional endpoints may opt out of global auth latching."""
        url = BASE + path
        with self._state_lock:
            token = self._token
            auth_failed = self.auth_failed
        if not token:
            raise UpstoxError("UPSTOX_ACCESS_TOKEN is not configured")
        if auth_failed:
            raise UpstoxError("Upstox authentication is rejected; replace the Analytics Token")

        last = None
        for attempt in range(max(1, retries)):
            self.budget.acquire()
            try:
                headers = {"Authorization": f"Bearer {token}"}
                if api_version:
                    headers["Api-Version"] = api_version
                r = self._session().get(
                    url, params=params, headers=headers,
                    timeout=(5, 15),
                )
            except requests.RequestException as e:
                last = UpstoxError(f"network error: {type(e).__name__}")
                with self._state_lock:
                    self.failure_count += 1
                    self.last_error = str(last)
                if attempt + 1 < retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                break

            with self._state_lock:
                self.request_count += 1
                self.last_status = r.status_code

            if r.status_code in (401, 403):
                with self._state_lock:
                    if latch_auth:
                        self.auth_failed = True
                        self.auth_status = r.status_code
                    self.failure_count += 1
                    self.last_error = f"authentication rejected ({r.status_code})"
                if latch_auth:
                    raise UpstoxError(f"auth error {r.status_code}: replace UPSTOX_ACCESS_TOKEN")
                raise UpstoxError(f"optional endpoint auth error {r.status_code}")

            if r.status_code == 429:
                last = UpstoxError("429 rate limited")
                delay = self._retry_after(r.headers.get("Retry-After"), 2.0 * (attempt + 1))
                with self._state_lock:
                    self.failure_count += 1
                    self.last_error = str(last)
                if attempt + 1 < retries:
                    time.sleep(delay)
                    continue
                break

            if r.status_code in (408, 425) or 500 <= r.status_code <= 599:
                last = UpstoxError(f"temporary HTTP {r.status_code}")
                with self._state_lock:
                    self.failure_count += 1
                    self.last_error = str(last)
                if attempt + 1 < retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                break

            if r.status_code != 200:
                with self._state_lock:
                    self.failure_count += 1
                    self.last_error = f"HTTP {r.status_code}"
                raise UpstoxError(f"{r.status_code}: {r.text[:200]}")

            try:
                payload = r.json()
                if not isinstance(payload, dict):
                    raise ValueError("response JSON root is not an object")
            except (ValueError, json.JSONDecodeError) as e:
                last = UpstoxError(f"invalid JSON response: {type(e).__name__}")
                with self._state_lock:
                    self.failure_count += 1
                    self.last_error = str(last)
                if attempt + 1 < retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                break

            with self._state_lock:
                self.last_success_at = datetime.now(IST).isoformat(timespec="seconds")
                self.last_error = ""
                self.last_status = r.status_code
            return payload

        raise UpstoxError(f"failed after {max(1, retries)} tries: {last}")

    @staticmethod
    def _frame(candles: list) -> pd.DataFrame:
        if not candles:
            return pd.DataFrame(columns=OHLCV)
        rows = []
        for candle in candles:
            if not isinstance(candle, (list, tuple)) or len(candle) < 6:
                continue
            # The API may omit open_interest; preserve the standard six OHLCV fields.
            try:
                ts = pd.to_datetime(candle[0], utc=True).tz_convert(IST)
                o, h, low, close, volume = [float(x) for x in candle[1:6]]
                oi = float(candle[6]) if len(candle) > 6 and candle[6] is not None else float("nan")
            except (TypeError, ValueError, OverflowError):
                continue
            vals = (o, h, low, close, volume)
            if not all(math.isfinite(x) for x in vals):
                continue
            if volume < 0 or h < max(o, close, low) or low > min(o, close, h):
                continue
            rows.append((ts, o, h, low, close, volume, oi))
        if not rows:
            return pd.DataFrame(columns=OHLCV)
        df = pd.DataFrame(rows, columns=["ts"] + OHLCV).set_index("ts").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return df

    def intraday(self, key: str, interval: int = 5) -> pd.DataFrame:
        d = self._get(f"/v3/historical-candle/intraday/{quote(key, safe='')}/minutes/{interval}")
        return self._frame((d.get("data") or {}).get("candles") or [])

    def historical(self, key: str, from_date, to_date, interval: int = 5) -> pd.DataFrame:
        path = (f"/v3/historical-candle/{quote(key, safe='')}/minutes/{interval}/"
                f"{to_date:%Y-%m-%d}/{from_date:%Y-%m-%d}")
        d = self._get(path)
        return self._frame((d.get("data") or {}).get("candles") or [])

    def ltp(self, keys: list[str]) -> dict:
        """Best-effort LTP lookup (used only for the option premium in the alert)."""
        try:
            # LTP is a convenience field in Telegram, not a prerequisite for a
            # signal. The v2 endpoint requires Api-Version: 2.0. Its auth failure
            # must never latch the shared candle-data client into a dead state.
            d = self._get(
                "/v2/market-quote/ltp",
                {"instrument_key": ",".join(keys)},
                retries=2,
                latch_auth=False,
                api_version="2.0",
            )
            out = {}
            for k, v in (d.get("data") or {}).items():
                if not isinstance(v, dict):
                    continue
                px = v.get("last_price", v.get("ltp"))
                tok = v.get("instrument_token")
                if px is not None:
                    try:
                        px = float(px)
                        if math.isfinite(px) and px > 0:
                            out[tok or k] = px
                    except (TypeError, ValueError):
                        continue
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
    with requests.get(MASTER_URL.format(seg=segment_file), stream=True, timeout=(10, 120)) as r:
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
    except (TypeError, ValueError, OverflowError):
        try:
            t = pd.Timestamp(v)
            return (t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)).to_pydatetime()
        except Exception:
            return None
