"""Flask app (gunicorn entry: app.web:app). Run with ONE worker - the scheduler lives in-process.

  GET /health                         liveness
  GET /status                         universe, API budget, auth and data diagnostics
  GET /diag                           per-module last cycle: scope, symbols, signals, reject reasons
  GET /stats?days=30                  win rate / net R per module from tracked alerts
  GET /force?module=normal|orb|mcx|all evaluate NOW ignoring clock windows (testing)
  GET /test-telegram                  Use Authorization: Bearer ADMIN_TOKEN for admin routes.
"""
import hmac
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
    # Fail closed when no admin secret is configured; never accept a public default.
    expected = (ADMIN_TOKEN or "").strip()
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    # Keep the query parameter temporarily for backward compatibility. Prefer the
    # Authorization header because query strings are commonly retained in logs.
    supplied = supplied or request.args.get("token", "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _init():
    global engine, _started
    with _start_lock:
        if _started:
            return
        try:
            store = make_store(config.DATABASE_URL)
            candidate = Engine(UpstoxClient(), store)
            if not config.UPSTOX_TOKEN:
                log.error("UPSTOX_ACCESS_TOKEN is empty - data calls will fail")
            if config.RUN_SCHEDULER:
                scheduler.start(candidate)
            engine = candidate
            if config.RUN_SCHEDULER:
                try:
                    send(f"<b>✅ Signal bot started {datetime.now(IST):%d %b %H:%M} IST · store {store.kind}</b>", silent=True)
                except Exception:
                    log.exception("startup notification failed; scheduler remains active")
            if config.KEEPALIVE and os.getenv("RENDER_EXTERNAL_URL"):
                threading.Thread(target=_keepalive, daemon=True).start()
            _started = True
        except Exception:
            engine = None
            _started = False
            log.exception("application startup failed; a later request may retry initialization")
            raise


def _keepalive():
    url = os.environ["RENDER_EXTERNAL_URL"].rstrip("/") + "/health"
    while True:
        time.sleep(600)
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
    result = engine.status()
    if hasattr(engine.client, "diagnostics"):
        result["upstox"] = engine.client.diagnostics()
    return jsonify(result)


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
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        return jsonify(error="days must be an integer from 1 to 365"), 400
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
    if mod not in ("all", "normal", "orb", "mcx"):
        return jsonify(error="module must be all, normal, orb or mcx"), 400
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
