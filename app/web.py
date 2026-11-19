"""FastAPI application for the Normal NSE F&O signal bot.

Exactly one Gunicorn/Uvicorn worker is required because the scanner is in-process.
The strategy remains in app.mod_normal; this module only hosts the application.
"""
import hmac
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import requests
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from . import config, scheduler
from .config import ADMIN_TOKEN, IST
from .db import make_store
from .engine import Engine
from .telegram import send
from .telegram_polling import TelegramPoller, get_persistent_state
from .upstox import UpstoxClient

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("web")

engine = None
store = None
_started = False
_start_lock = threading.Lock()
_scheduler_stop = None
_telegram_poller = None
_keepalive_stop = threading.Event()


def _auth(request: Request) -> bool:
    """Fail closed; bearer header preferred, legacy query token retained."""
    expected = (ADMIN_TOKEN or "").strip()
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    supplied = supplied or request.query_params.get("token", "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _init():
    global engine, store, _started, _scheduler_stop, _telegram_poller
    with _start_lock:
        if _started:
            return
        try:
            if config.REQUIRE_PERSISTENT_DB and not config.DATABASE_URL:
                raise RuntimeError("NEON_DATABASE_URL is required; persistent storage is mandatory")
            candidate_store = make_store(config.DATABASE_URL)
            candidate = Engine(UpstoxClient(), candidate_store)
            candidate.paused = bool(get_persistent_state(candidate_store, "paused", False))
            if not config.UPSTOX_TOKEN:
                log.error("UPSTOX_ACCESS_TOKEN is empty - data calls will fail")
            if config.RUN_SCHEDULER:
                _scheduler_stop = scheduler.start(candidate)
            engine, store = candidate, candidate_store
            if config.RUN_SCHEDULER and config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID:
                _telegram_poller = TelegramPoller(candidate, candidate_store)
                _telegram_poller.start()
            if config.RUN_SCHEDULER:
                try:
                    send(f"<b>✅ Normal F&amp;O signal bot started {datetime.now(IST):%d %b %H:%M} IST · store {candidate_store.kind}</b>", silent=True)
                except Exception:
                    log.exception("startup notification failed; scanner remains active")
            if config.KEEPALIVE and os.getenv("RENDER_EXTERNAL_URL"):
                threading.Thread(target=_keepalive, name="keepalive", daemon=True).start()
            _started = True
            log.info("application initialized; store=%s scheduler=%s", candidate_store.kind, config.RUN_SCHEDULER)
        except Exception:
            engine = None
            store = None
            _started = False
            log.exception("application startup failed; a later request may retry initialization")
            raise


def _keepalive():
    url = os.environ["RENDER_EXTERNAL_URL"].rstrip("/") + "/health"
    while not _keepalive_stop.wait(600):
        try:
            requests.get(url, timeout=10)
        except requests.RequestException:
            pass


def _require_engine():
    try:
        _init()
    except Exception as exc:
        return None, JSONResponse(status_code=503, content={"error": "service_initialization_failed"})
    if engine is None:
        return None, JSONResponse(status_code=503, content={"error": "service_not_initialized"})
    return engine, None


def _recent_signals(limit: int = 20):
    if store is None:
        return []
    limit = max(1, min(100, limit))
    if getattr(store, "kind", "") == "postgres":
        rows = store._run(
            """SELECT id, module, symbol, side, bar_ts, entry, sl, tp1, tp2,
                      score, trig, status, r_result, mfe_r, mae_r, bars_held, details
               FROM signals ORDER BY id DESC LIMIT %s""",
            (limit,), "all")
        keys = ("id", "module", "symbol", "side", "bar_ts", "entry", "sl", "tp1",
                "tp2", "score", "trigger", "status", "r_result", "mfe_r", "mae_r", "bars_held", "details")
        return [dict(zip(keys, row)) for row in rows]
    rows = sorted(getattr(store, "rows", []), key=lambda row: row.get("id", 0), reverse=True)
    return [{key: row.get(key) for key in (
        "id", "module", "symbol", "side", "bar_ts", "entry", "sl", "tp1",
        "tp2", "score", "trig", "status", "r_result", "mfe_r", "mae_r", "bars_held", "details")} for row in rows[:limit]]


def _safe_config():
    return {
        "timezone": "Asia/Kolkata",
        "candle_interval_minutes": config.ENGINE.candle_min,
        "fetch_delay_seconds": config.ENGINE.fetch_delay_sec,
        "stale_max_bars": config.ENGINE.stale_max_bars,
        "signal_start": config.NORMAL.first_signal.strftime("%H:%M"),
        "signal_stop": config.NORMAL.last_entry.strftime("%H:%M"),
        "session_end": config.NORMAL.session_end.strftime("%H:%M"),
        "adx_min": config.NORMAL.adx_min,
        "ema_fast": config.IND.ema_fast,
        "ema_slow": config.IND.ema_slow,
        "rsi_period": config.IND.rsi_n,
        "atr_period": config.IND.atr_n,
        "sl_atr": config.NORMAL.sl_atr,
        "tp1_r": config.NORMAL.tp1_r,
        "tp2_r": config.NORMAL.tp2_r,
        "option_market_snapshot": config.OPTION_MARKET_SNAPSHOT,
        "option_liquidity_checks": config.OPTION_LIQUIDITY_CHECKS,
        "option_min_volume": config.OPTION_MIN_VOLUME,
        "option_min_oi": config.OPTION_MIN_OI,
        "option_max_spread_pct": config.OPTION_MAX_SPREAD_PCT,
        "option_max_quote_age_sec": config.OPTION_MAX_QUOTE_AGE_SEC,
        "scheduler_enabled": config.RUN_SCHEDULER,
        "signal_only": True,
        "orders_enabled": False,
    }


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.getenv("AUTOSTART", "1") == "1":
        threading.Thread(target=_init, name="bot-initializer", daemon=True).start()
    yield
    _keepalive_stop.set()
    if _telegram_poller is not None:
        _telegram_poller.stop()
    if _scheduler_stop is not None:
        _scheduler_stop.set()
    if engine is not None and getattr(engine, "market_stream", None) is not None:
        engine.market_stream.stop()
    if store is not None and hasattr(store, "close"):
        try:
            store.close()
        except Exception:
            log.exception("database pool shutdown failed")


app = FastAPI(title="Normal NSE F&O Signal Bot", version="2.0.0", lifespan=lifespan)


@app.get("/")
@app.get("/health")
def health():
    # Liveness remains immediate and never initializes database/provider services.
    return {"ok": True, "time": datetime.now(IST).isoformat(timespec="seconds"), "initialized": _started}


@app.get("/status")
def status(request: Request):
    candidate, error = _require_engine()
    if error:
        return error
    if not _auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    result = candidate.status()
    if hasattr(candidate.client, "diagnostics"):
        result["upstox"] = candidate.client.diagnostics()
    if getattr(candidate, "market_stream", None) is not None:
        result["market_feed"] = candidate.market_stream.diagnostics()
    result["paused"] = bool(getattr(candidate, "paused", False))
    result["telegram_polling"] = bool(_telegram_poller and _telegram_poller.running)
    result["database"] = {"kind": store.kind, "persistent": store.kind == "postgres"}
    return jsonable_encoder(result)


@app.get("/diag")
@app.get("/diagnostics")
def diagnostics(request: Request):
    candidate, error = _require_engine()
    if error:
        return error
    if not _auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return jsonable_encoder(candidate.diag_json())


@app.get("/signals")
def signals(request: Request, limit: int = 20):
    if not _auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    _, error = _require_engine()
    if error:
        return error
    return jsonable_encoder(_recent_signals(limit))


@app.get("/stats")
def stats(request: Request, days: int = 30):
    candidate, error = _require_engine()
    if error:
        return error
    if not _auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    days = max(1, min(365, days))
    since = (datetime.now(IST) - timedelta(days=days)).date()
    rows = candidate.store.stats(since)
    for row in rows:
        row["win_rate_pct"] = round(100 * row["wins"] / row["closed"], 1) if row["closed"] else None
        row["avg_r"] = round(row["sum_r"] / row["closed"], 3) if row["closed"] else None
        row["expectancy_r"] = row["avg_r"]
    return jsonable_encoder(rows)


@app.get("/symbols")
def symbols():
    if engine is None or engine.uni is None:
        return {"ready": False, "configured_symbols": 0, "validated_symbols": 0, "active_symbols": []}
    items = engine.uni.normal
    return {
        "ready": True,
        "configured_symbols": len(items),
        "validated_symbols": len(items),
        "invalid_symbols": engine.uni.dropped,
        "active_symbols": [{"symbol": item.symbol, "instrument_key": item.key, "is_index": item.is_index} for item in items],
    }


@app.get("/config")
def config_view():
    return _safe_config()


@app.post("/pause")
def pause(request: Request):
    candidate, error = _require_engine()
    if error:
        return error
    if not _auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    candidate.paused = True
    from .telegram_polling import set_persistent_state
    set_persistent_state(candidate.store, "paused", True)
    return {"ok": True, "paused": True}


@app.post("/resume")
def resume(request: Request):
    candidate, error = _require_engine()
    if error:
        return error
    if not _auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    candidate.paused = False
    from .telegram_polling import set_persistent_state
    set_persistent_state(candidate.store, "paused", False)
    return {"ok": True, "paused": False}


@app.get("/force")
def force(request: Request, module: str = "all"):
    candidate, error = _require_engine()
    if error:
        return error
    if not _auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    if module not in ("all", "normal"):
        return JSONResponse(status_code=400, content={"error": "module must be all or normal"})

    def run():
        result = candidate.run_boundary(datetime.now(IST), only=None if module == "all" else module, force=True)
        log.info("forced run: %s", {name: data.get("signals") for name, data in result.items() if isinstance(data, dict)})

    threading.Thread(target=run, name="forced-scan", daemon=True).start()
    return {"started": True, "module": module, "note": "results in Telegram and /diagnostics"}


@app.get("/test-telegram")
def test_telegram(request: Request):
    _, error = _require_engine()
    if error:
        return error
    if not _auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return {"sent": send("<b>Telegram test OK</b>")}
