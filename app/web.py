"""FastAPI application for the Normal NSE F&O signal bot.

Exactly one Gunicorn/Uvicorn worker is required because the scanner is in-process.
The strategy remains in app.mod_normal; this module only hosts the application.
"""
import hmac
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta

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
from .symbols import EXPECTED_UNIVERSE_SIZE

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
        "upstox_stream_enabled": config.UPSTOX_STREAM_ENABLED,
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


@app.get("/ready")
def readiness():
    """Readiness probe for the Render router: require working config and persistence.

    /health remains a pure liveness endpoint. This endpoint deliberately fails
    closed when the scanner could not authenticate or resolve its fixed universe.
    """
    def unavailable(reason):
        return JSONResponse(status_code=503, content={"ready": False, "reason": reason})

    if not _started or engine is None or store is None:
        return unavailable("application_not_initialized")
    if not config.RUN_SCHEDULER:
        return unavailable("scheduler_disabled")
    if not config.ENABLE_NORMAL:
        return unavailable("normal_strategy_disabled")
    if not config.UPSTOX_TOKEN:
        return unavailable("upstox_token_missing")
    if not config.ADMIN_TOKEN:
        return unavailable("admin_token_missing")
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return unavailable("telegram_not_configured")

    scheduler_thread = getattr(_scheduler_stop, "thread", None)
    if _scheduler_stop is None or scheduler_thread is None or not scheduler_thread.is_alive():
        return unavailable("scheduler_not_running")

    poller = _telegram_poller
    poller_thread = getattr(poller, "thread", None) if poller is not None else None
    if poller is None or not bool(getattr(poller, "running", False)) or poller_thread is None or not poller_thread.is_alive():
        return unavailable("telegram_poller_not_running")
    poller_is_leader = bool(getattr(poller, "leader_lock_acquired", False))
    poller_error = str(getattr(poller, "last_error", ""))
    poller_standby = poller_error.startswith("standby: another instance owns Telegram polling")
    poller_conflict = poller_error.startswith("Telegram getUpdates conflict")
    if bool(getattr(poller, "auth_failed", False)):
        return unavailable("telegram_auth_rejected")
    # During Render's rolling deploy, the old instance may retain the singleton
    # lock until the new instance passes its health check. Standby and a 409
    # from that old, pre-lock poller must not deadlock the deployment. Signal
    # delivery is independent of getUpdates; expose the state in diagnostics.
    if not poller_is_leader and not poller_standby:
        return unavailable("telegram_poller_leadership_unavailable")
    if poller_is_leader and not poller_conflict:
        last_telegram_success = getattr(poller, "last_success_at", None)
        if not last_telegram_success:
            return unavailable("telegram_not_yet_connected")
        try:
            telegram_success_at = datetime.fromisoformat(last_telegram_success)
            if telegram_success_at.tzinfo is None:
                telegram_success_at = telegram_success_at.replace(tzinfo=IST)
            if datetime.now(IST) - telegram_success_at > timedelta(seconds=90):
                return unavailable("telegram_connection_stale")
        except (TypeError, ValueError):
            return unavailable("telegram_health_timestamp_invalid")

    client = getattr(engine, "client", None)
    if bool(getattr(client, "auth_failed", False)):
        return unavailable("upstox_auth_rejected")

    now = datetime.now(IST)

    def age_seconds(value):
        if not value:
            return None
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=IST)
            return max(0.0, (now - stamp.astimezone(IST)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

    market_open = (
        now.weekday() < 5
        and time(9, 15) <= now.time() <= time(15, 30)
    )
    entry_scan_window = (
        now.weekday() < 5
        and config.NORMAL.first_signal <= now.time() <= config.NORMAL.last_entry
    )
    scanner_health = {
        "last_cycle_started_at": getattr(engine, "last_cycle_started_at", None),
        "last_cycle_completed_at": getattr(engine, "last_cycle_completed_at", None),
        "last_successful_cycle_at": getattr(engine, "last_successful_cycle_at", None),
        "last_cycle_error": getattr(engine, "last_cycle_error", ""),
        "last_cycle_result": getattr(engine, "last_cycle_result", {}),
        "paused": bool(getattr(engine, "paused", False)),
    }
    provider_health = {}

    universe = getattr(engine, "uni", None)
    if universe is not None:
        configured_symbols = list(getattr(universe, "configured_symbols", []))
        active_symbols = [
            str(getattr(item, "symbol", "")) for item in getattr(universe, "normal", [])
        ]
        if (
            not bool(getattr(universe, "ready", False))
            or len(configured_symbols) != EXPECTED_UNIVERSE_SIZE
            or len(active_symbols) != EXPECTED_UNIVERSE_SIZE
            or len(set(configured_symbols)) != EXPECTED_UNIVERSE_SIZE
            or set(configured_symbols) != set(active_symbols)
            or "" in active_symbols
        ):
            return unavailable("universe_validation_failed")
        now = datetime.now(IST)
        if (
            now.weekday() < 5
            and now.time() >= time(7, 30)
            and getattr(universe, "built_on", "") != now.strftime("%Y-%m-%d")
        ):
            return unavailable("universe_stale")
    if universe is None:
        # On trading weekdays the master must be resolved before the normal
        # session begins. Before 07:30 and on weekends, no universe is expected.
        now = datetime.now(IST)
        if now.weekday() < 5 and now.time() >= time(7, 30):
            if str(getattr(engine, "last_error", "")).startswith("universe:"):
                return unavailable("universe_load_failed")
            return unavailable("universe_not_initialized")

    if entry_scan_window:
        if not scanner_health["last_cycle_completed_at"]:
            return unavailable("scanner_cycle_not_yet_completed")
        cycle_age = age_seconds(scanner_health["last_cycle_completed_at"])
        if cycle_age is None:
            return unavailable("scanner_cycle_timestamp_invalid")
        if cycle_age > 8 * 60:
            return unavailable("scanner_cycle_stale")
        if scanner_health["last_cycle_error"]:
            return unavailable("scanner_cycle_failed")
        successful_age = age_seconds(scanner_health["last_successful_cycle_at"])
        if successful_age is None or successful_age > 8 * 60:
            return unavailable("scanner_has_no_recent_successful_cycle")
        normal_result = (scanner_health["last_cycle_result"] or {}).get("normal", {})
        try:
            expected = int(normal_result.get("symbols") or 0)
            loaded = int(normal_result.get("loaded") or 0)
        except (AttributeError, TypeError, ValueError):
            return unavailable("scanner_result_invalid")
        if expected != EXPECTED_UNIVERSE_SIZE:
            return unavailable("scanner_symbol_count_mismatch")
        minimum_loaded = max(1, int(expected * 0.90))
        if loaded < minimum_loaded:
            return unavailable("scanner_data_incomplete")

        try:
            provider_health = client.diagnostics() if hasattr(client, "diagnostics") else {}
        except Exception:
            log.exception("readiness Upstox diagnostics failed")
            return unavailable("upstox_diagnostics_unavailable")
        if not isinstance(provider_health, dict):
            return unavailable("upstox_diagnostics_invalid")
        provider_success = provider_health.get("last_success_at") or getattr(client, "last_success_at", None)
        provider_age = age_seconds(provider_success)
        if provider_age is None or provider_age > 8 * 60:
            return unavailable("upstox_rest_data_stale")

    feed_health = None
    if market_open and config.UPSTOX_STREAM_ENABLED:
        feed = getattr(engine, "market_stream", None)
        if feed is None or not hasattr(feed, "diagnostics"):
            return unavailable("market_feed_not_started")
        try:
            feed_health = feed.diagnostics()
        except Exception:
            log.exception("readiness market-feed diagnostics failed")
            return unavailable("market_feed_diagnostics_unavailable")
        if not isinstance(feed_health, dict):
            return unavailable("market_feed_diagnostics_invalid")
        if not feed_health.get("connected") or not feed_health.get("thread_alive"):
            return unavailable("market_feed_disconnected")
        if int(feed_health.get("subscribed_symbols") or 0) < EXPECTED_UNIVERSE_SIZE:
            return unavailable("market_feed_subscription_incomplete")
        feed_age = feed_health.get("last_quote_age_sec")
        if feed_age is None or feed_age > 120:
            return unavailable("market_feed_stale")

    persistent = getattr(store, "kind", "") == "postgres"
    if config.REQUIRE_PERSISTENT_DB and not persistent:
        return unavailable("persistent_database_required")
    if persistent:
        try:
            store._run("SELECT 1", fetch="one")
        except Exception:
            log.exception("readiness database check failed")
            return unavailable("database_unavailable")

    return {
        "ready": True,
        "initialized": True,
        "scheduler": {"enabled": True, "running": True},
        "scanner": {
            "last_cycle_started_at": scanner_health["last_cycle_started_at"],
            "last_cycle_completed_at": scanner_health["last_cycle_completed_at"],
            "last_successful_cycle_at": scanner_health["last_successful_cycle_at"],
            "last_cycle_error": scanner_health["last_cycle_error"],
            "last_cycle_result": scanner_health["last_cycle_result"],
            "paused": scanner_health["paused"],
            "strategy_enabled": True,
        },
        "upstox": {
            "token_configured": True,
            "auth_failed": False,
            "last_success_at": (
                provider_health.get("last_success_at")
                or getattr(client, "last_success_at", None)
            ),
        },
        "market_feed": feed_health,
        "telegram": {
            "credentials_configured": True,
            "poller_running": True,
            "leader": poller_is_leader,
            "state": "leader" if poller_is_leader else "standby",
            "last_error": poller_error or None,
            "auth_failed": bool(getattr(poller, "auth_failed", False)),
            "last_success_at": getattr(poller, "last_success_at", None),
        },
        "universe": {
            "validated": universe is not None,
            "configured_symbols": len(getattr(universe, "configured_symbols", [])) if universe else 0,
            "validated_symbols": len(getattr(universe, "normal", [])) if universe else 0,
            "expected_symbols": EXPECTED_UNIVERSE_SIZE,
        },
        "database": {"kind": getattr(store, "kind", "unknown"), "persistent": persistent},
    }


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
def stats(request: Request, days: int = 30, detailed: bool = False):
    candidate, error = _require_engine()
    if error:
        return error
    if not _auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    days = max(1, min(365, days))
    since = (datetime.now(IST) - timedelta(days=days)).date()
    if detailed:
        return jsonable_encoder(candidate.store.performance_report(since))
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
    configured = list(engine.uni.configured_symbols)
    active = {item.symbol for item in items}
    duplicates = sorted({symbol for symbol in configured if configured.count(symbol) > 1})
    missing = sorted(set(configured) - active)
    return {
        "ready": bool(getattr(engine.uni, "ready", False)) and len(configured) == 150 and len(items) == 150 and not duplicates and not missing,
        "configured_symbols": len(configured),
        "configured_symbol_list": configured,
        "validated_symbols": len(items),
        "missing_symbols": missing,
        "invalid_symbols": engine.uni.dropped,
        "duplicate_symbols": duplicates,
        "validation_errors": getattr(engine.uni, "validation_errors", []),
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
