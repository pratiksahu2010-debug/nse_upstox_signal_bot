"""Telegram sender + alert formatter (HTML, every detail bold)."""
import html
import logging
import threading
import time
from typing import Optional

import pandas as pd
import requests

from .common import Signal
from .config import DRY_RUN, TELEGRAM_CHAT_ID, TELEGRAM_TOKEN

log = logging.getLogger("telegram")
_lock = threading.Lock()
_last = [0.0]


def _b(x) -> str:
    return f"<b>{html.escape(str(x))}</b>"


def fmt_price(x: float) -> str:
    return f"{x:,.2f}"


def format_signal(sig: Signal) -> str:
    is_call = sig.side == "LONG"
    dot = "🟢" if is_call else "🔴"
    label = "CALL (CE)" if is_call else "PUT (PE)"
    risk = sig.risk
    rr1 = abs(sig.tp1 - sig.entry) / risk if risk else 0
    rr2 = abs(sig.tp2 - sig.entry) / risk if risk else 0
    close_time = (sig.bar_ts + pd.Timedelta(minutes=5)).strftime("%H:%M")
    lines = [
        f"{dot} {_b(sig.symbol)} · {_b(label)}",
        _b(f"5m candle closed {close_time} IST"),
        _b(f"Entry {fmt_price(sig.entry)}"),
        _b(f"Stop loss {fmt_price(sig.sl)} · {risk / sig.entry * 100:.2f}% risk"),
        _b(f"Target 1 {fmt_price(sig.tp1)} ({rr1:.1f}R) · Target 2 {fmt_price(sig.tp2)} ({rr2:.1f}R)"),
        _b("ATR-based levels · signal only, manual execution"),
    ]
    if sig.option:
        option = sig.option
        premium = f" · LTP {fmt_price(option['ltp'])}" if option.get("ltp") else ""
        depth = ""
        if option.get("bid") and option.get("ask"):
            depth = f" · Bid/Ask {fmt_price(option['bid'])}/{fmt_price(option['ask'])}"
        spread = f" · Spread {option['spread_pct']:.2f}%" if option.get("spread_pct") is not None else ""
        liquidity = ""
        if option.get("volume") is not None or option.get("oi") is not None:
            liquidity = f" · Vol {option.get('volume', 'n/a')} · OI {option.get('oi', 'n/a')}"
        lines.append(_b(f"Option {option['tsym']} · lot {option['lot']}{premium}{depth}{spread}{liquidity}"))
    lines.append(_b("Conditions: " + " | ".join(sig.reasons)))
    if sig.forced:
        lines.append(_b("FORCED TEST"))
    return "\n".join(lines)

def send(text: str, silent: bool = False) -> bool:
    if DRY_RUN or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("[telegram %s]\n%s", "dry-run" if DRY_RUN else "not configured", text)
        return DRY_RUN
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True, "disable_notification": silent}
    for attempt in range(4):
        with _lock:  # Telegram: ~1 msg/s per chat
            wait = 1.1 - (time.monotonic() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.monotonic()
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                # Telegram may return a malformed/non-JSON body during overload.
                # Keep retry bounded so a bad response cannot crash delivery or
                # block the worker for an unbounded period.
                try:
                    body = r.json()
                    parameters = body.get("parameters", {}) if isinstance(body, dict) else {}
                    retry_after = float(parameters.get("retry_after", 3))
                except (ValueError, TypeError, AttributeError):
                    retry_after = 3.0
                if not (retry_after > 0 and retry_after < float("inf")):
                    retry_after = 3.0
                time.sleep(max(1.0, min(30.0, retry_after)) + 0.5)
                continue
            log.error("telegram %s: %s", r.status_code, r.text[:200])
            if r.status_code in (400, 401, 403, 404):
                return False
        except requests.RequestException as e:
            log.error("telegram error: %s", e)
            time.sleep(2 * (attempt + 1))
    return False
