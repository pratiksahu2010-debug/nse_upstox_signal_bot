"""Clock loop: wakes at every 5-minute candle close (+ FETCH_DELAY_SEC) and runs the modules.

State is rebuilt from the API + Postgres on every start, so Render free-tier restarts and
sleep/wake cycles are harmless: a late wake-up only evaluates the newest closed candle and
stale signals (older than STALE_MAX_BARS) are suppressed rather than sent late.
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from .config import ENGINE, IST, UPSTOX_TOKEN
from .engine import Engine, floor5

log = logging.getLogger("scheduler")


def _notify_auth_state(engine: Engine, now: datetime):
    missing = not UPSTOX_TOKEN
    rejected = bool(getattr(engine.client, "auth_failed", False))
    if not (missing or rejected):
        engine.auth_alerted_at = None
        return
    last = getattr(engine, "auth_alerted_at", None)
    if last is not None and now - last < timedelta(minutes=5):
        return
    reason = "UPSTOX_ACCESS_TOKEN is missing" if missing else "Upstox rejected UPSTOX_ACCESS_TOKEN"
    message = (
        f"<b>⚠️ Upstox authentication problem</b>\n{reason}. "
        "Update the Render secret and restart the service. Scanner data may be unavailable; "
        "no broker orders are placed."
    )
    try:
        engine.notify(message, silent=False)
    except Exception:
        log.exception("could not send Upstox authentication alert")
    engine.auth_alerted_at = now
    log.error("Upstox authentication problem: %s", reason)


def _next_due(now: datetime) -> datetime:
    return floor5(now) + timedelta(minutes=ENGINE.candle_min, seconds=ENGINE.fetch_delay_sec)


def loop(engine: Engine, stop: threading.Event):
    log.info("scheduler started")
    last_run = None
    while not stop.is_set():
        now = datetime.now(IST)
        _notify_auth_state(engine, now)
        try:
            engine.housekeeping(now)
        except Exception:
            log.exception("housekeeping failed")
        # candle that just closed and is past the fetch delay?
        bar_close = floor5(now)
        due = bar_close + timedelta(seconds=ENGINE.fetch_delay_sec)
        if now >= due and last_run != bar_close and now.weekday() < 5:
            # Preserve the existing late-wake policy: skip boundaries that are
            # already too old, rather than issuing stale signals after a long sleep.
            if now - due >= timedelta(minutes=ENGINE.candle_min):
                last_run = bar_close
            else:
                try:
                    res = engine.run_boundary(now)
                    # A concurrent /force scan can hold the engine lock. Do not
                    # consume this candle boundary when run_boundary reports busy:
                    # retry on the next scheduler tick after the lock is released.
                    if not (isinstance(res, dict) and res.get("busy")):
                        last_run = bar_close
                    log.info("cycle %s -> %s", bar_close.strftime("%H:%M"),
                             {m: (d.get("scope"), d.get("loaded"), d.get("signals")) for m, d in res.items()
                              if isinstance(d, dict)})
                except Exception:
                    # Avoid a tight retry loop for unexpected failures. The engine's
                    # own per-cycle diagnostics report module-level failures.
                    last_run = bar_close
                    log.exception("run_boundary failed")
        stop.wait(2.0)
    if engine.market_stream is not None:
        engine.market_stream.stop()
    log.info("scheduler stopped")


def start(engine: Engine) -> threading.Event:
    stop = threading.Event()
    t = threading.Thread(target=loop, args=(engine, stop), name="scheduler", daemon=True)
    # Keep the worker handle alongside the existing stop Event so readiness
    # can detect an unexpectedly terminated scheduler without changing callers.
    stop.thread = t
    t.start()
    return stop
