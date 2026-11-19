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


def _next_due(now: datetime) -> datetime:
    return floor5(now) + timedelta(minutes=ENGINE.candle_min, seconds=ENGINE.fetch_delay_sec)


def loop(engine: Engine, stop: threading.Event):
    log.info("scheduler started")
    last_run = None
    while not stop.is_set():
        now = datetime.now(IST)
        try:
            engine.housekeeping(now)
        except Exception:
            log.exception("housekeeping failed")
        # Start the V3 live feed only after the validated instrument universe exists.
        # Closed 5m REST candles remain authoritative for strategy decisions.
        if engine.uni and UPSTOX_TOKEN:
            try:
                from .market_stream import UpstoxMarketStream
                if engine.market_stream is None:
                    engine.market_stream = UpstoxMarketStream(engine.client)
                    engine.market_stream.start([item.key for item in engine.uni.normal])
                else:
                    engine.market_stream.set_keys([item.key for item in engine.uni.normal])
            except Exception:
                log.exception("could not start/update Upstox market feed")
        # candle that just closed and is past the fetch delay?
        bar_close = floor5(now)
        due = bar_close + timedelta(seconds=ENGINE.fetch_delay_sec)
        if now >= due and last_run != bar_close and now.weekday() < 5:
            last_run = bar_close
            if now - due < timedelta(minutes=ENGINE.candle_min):   # skip if we woke up very late
                try:
                    res = engine.run_boundary(now)
                    log.info("cycle %s -> %s", bar_close.strftime("%H:%M"),
                             {m: (d.get("scope"), d.get("loaded"), d.get("signals")) for m, d in res.items()
                              if isinstance(d, dict)})
                except Exception:
                    log.exception("run_boundary failed")
        stop.wait(2.0)
    if engine.market_stream is not None:
        engine.market_stream.stop()
    log.info("scheduler stopped")


def start(engine: Engine) -> threading.Event:
    stop = threading.Event()
    t = threading.Thread(target=loop, args=(engine, stop), name="scheduler", daemon=True)
    t.start()
    return stop
