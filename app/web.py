"""Flask app (gunicorn entry: app.web:app). Run with ONE worker - the scheduler lives in-process.

  GET /health                         liveness (point UptimeRobot / cron-job.org here every 5 min)
  GET /status?token=ADMIN_TOKEN       universe, API budget, bias, errors
  GET /diag?token=...                 per-module last cycle: scope, symbols, signals, reject reasons
  GET /stats?token=...&days=30        win rate / net R per module from tracked alerts
  GET /force?token=...&module=normal|orb|mcx|all   evaluate NOW ignoring clock windows (testing)
  GET /test-telegram?token=...
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, request

from . import config, scheduler
from .config import ADMIN_TOKEN, IST
from .db import make_store
from .engine import Engine
from .telegram import send
from .upstox import UpstoxClient

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("web")

app = Flask(__name__)
engine: Engine = None
_started = False
_start_lock = threading.Lock()


def _auth() -> bool:
    return request.args.get("token", "") == ADMIN_TOKEN


def _init():
    global engine, _started
    with _start_lock:
        if _started:
            return
        _started = True
        store = make_store(config.DATABASE_URL)
        engine = Engine(UpstoxClient(), store)
        if not config.UPSTOX_TOKEN:
            log.error("UPSTOX_ACCESS_TOKEN is empty - data calls will fail")
        if config.RUN_SCHEDULER:
            scheduler.start(engine)
            send(f"<b>✅ Signal bot started {datetime.now(IST):%d %b %H:%M} IST · store {store.kind}</b>", silent=True)
        if config.KEEPALIVE and os.getenv("RENDER_EXTERNAL_URL"):
            threading.Thread(target=_keepalive, daemon=True).start()


def _keepalive():
    url = os.environ["RENDER_EXTERNAL_URL"].rstrip("/") + "/health"
    while True:
        time.sleep(600)       # Render free web services sleep after 15 min without inbound traffic
        try:
            requests.get(url, timeout=10)
        except requests.RequestException:
            pass


@app.get("/")
@app.get("/health")
def health():
    _init()
    return jsonify(ok=True, time=datetime.now(IST).isoformat(timespec="seconds"))


@app.get("/status")
def status():
    _init()
    if not _auth():
        return jsonify(error="unauthorized"), 401
    return jsonify(engine.status())


@app.get("/diag")
def diag():
    _init()
    if not _auth():
        return jsonify(error="unauthorized"), 401
    return jsonify(engine.diag_json())


@app.get("/stats")
def stats():
    _init()
    if not _auth():
        return jsonify(error="unauthorized"), 401
    days = int(request.args.get("days", 30))
    since = (datetime.now(IST) - timedelta(days=days)).date()
    rows = engine.store.stats(since)
    for r in rows:
        r["win_rate_pct"] = round(100 * r["wins"] / r["closed"], 1) if r["closed"] else None
        r["avg_r"] = round(r["sum_r"] / r["closed"], 3) if r["closed"] else None
    return jsonify(rows)


@app.get("/force")
def force():
    _init()
    if not _auth():
        return jsonify(error="unauthorized"), 401
    mod = request.args.get("module", "all")
    only = None if mod == "all" else mod

    def run():
        res = engine.run_boundary(datetime.now(IST), only=only, force=True)
        log.info("forced run: %s", {m: d.get("signals") for m, d in res.items() if isinstance(d, dict)})
    threading.Thread(target=run, daemon=True).start()
    return jsonify(started=True, module=mod, note="results in Telegram and /diag")


@app.get("/test-telegram")
def test_telegram():
    _init()
    if not _auth():
        return jsonify(error="unauthorized"), 401
    return jsonify(sent=send("<b>Telegram test OK</b>"))


# start the scheduler as soon as gunicorn imports the module (not only on first request)
if os.getenv("AUTOSTART", "1") == "1":
    threading.Thread(target=_init, daemon=True).start()
