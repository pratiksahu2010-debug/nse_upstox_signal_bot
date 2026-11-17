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
    long_ = sig.side == "LONG"
    dot = "🟢" if long_ else "🔴"
    info = sig.info
    cp = "CALL (CE)" if long_ else "PUT (PE)"
    head = f"{dot} {_b(sig.symbol)} / {_b(sig.side + (' → ' + cp if sig.module != 'mcx' else ''))}"
    risk = sig.risk
    mod = {"normal": "NORMAL", "orb": "ORB", "mcx": "MCX"}[sig.module]
    rr1 = abs(sig.tp1 - sig.entry) / risk if risk else 0
    rr2 = abs(sig.tp2 - sig.entry) / risk if risk else 0
    t = (sig.bar_ts + pd.Timedelta(minutes=5)).strftime("%H:%M")
    lines = [
        head,
        _b(f"{mod} · {sig.trigger} · score {sig.score}/100" + (" · FORCED TEST" if sig.forced else "")),
        _b(f"5m candle {t} closed"),
        _b(f"Entry {fmt_price(sig.entry)} (signal-bar close)"),
        _b(f"SL {fmt_price(sig.sl)} · risk {fmt_price(risk)} ({risk / sig.entry * 100:.2f}%)"),
        _b(f"TP1 {fmt_price(sig.tp1)} ({rr1:.1f}R) · TP2 {fmt_price(sig.tp2)} ({rr2:.1f}R)"),
        _b(f"Skip if price moves > {fmt_price(0.5 * sig.atr)} beyond entry before you enter"),
    ]
    if sig.option:
        o = sig.option
        prem = f" · LTP {fmt_price(o['ltp'])}" if o.get("ltp") else ""
        lines.append(_b(f"Option {o['tsym']} · lot {o['lot']}{prem}"))
    elif info and info.lot:
        lines.append(_b(f"Lot {info.lot}" + (f" · {info.expiry}" if info.expiry else "")))
    lines.append(_b("Why: " + " | ".join(sig.reasons)))
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
                time.sleep(float(r.json().get("parameters", {}).get("retry_after", 3)) + 0.5)
                continue
            log.error("telegram %s: %s", r.status_code, r.text[:200])
            if r.status_code in (400, 401, 403, 404):
                return False
        except requests.RequestException as e:
            log.error("telegram error: %s", e)
            time.sleep(2 * (attempt + 1))
    return False
