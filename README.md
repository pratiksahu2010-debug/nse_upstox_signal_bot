# Normal NSE F&O CALL/PUT Signal Bot

Signal-only NSE F&O application. It does not place, modify or execute broker orders.

## Runtime

- Python 3.11 and FastAPI.
- Gunicorn with Uvicorn worker; exactly one worker because the scanner is in-process.
- Upstox REST remains the source of completed 5-minute OHLCV candles and instrument master data.
- Upstox Market Data Feed V3 WebSocket provides live quote/feed-health observations. Live ticks do not create signals.
- Neon PostgreSQL is the production persistent store. NEON_DATABASE_URL is preferred; DATABASE_URL remains a compatibility alias. The database bootstrap is tracked through an idempotent schema_migrations version table.
- Telegram Bot API getUpdates long polling; no webhook. Missing or rejected Upstox credentials trigger throttled Telegram alerts; after replacing a Render secret, restart the service to reconnect with the new token.

## Strategy — preserved current implementation

There is one active strategy module: Normal NSE F&O. The existing strategy code in app/mod_normal.py has been kept unchanged.

CALL setup: EMA9 > EMA21, close > session VWAP, ADX14 >= 22, +DI > -DI, followed by RSI14 crossing upward through 52.

PUT setup: EMA9 < EMA21, close < session VWAP, ADX14 >= 22, -DI > +DI, followed by RSI14 crossing downward through 42.

ADX uses Wilder smoothing. N_ADX_MIN is hard-floored at 22 and may only be raised. The 15-minute EMA trend feature is calculated but is not an active setup gate in app/mod_normal.py.

The master prompt includes proposed strategy changes: 15-minute DI informational-only, a fixed 150-symbol list, score gate and different targets. They are not silently applied because the existing strategy was explicitly locked. The current 5-minute DI gate, RSI-cross trigger, universe resolver and ATR target parameters remain unchanged.

## Risk and state

Default underlying reference levels:
- Stop loss: 1.2 × 5-minute ATR(14).
- TP1: 1.5R.
- TP2: 2.0R.

The first completed candle observed after startup is a baseline. A qualifying setup can arm a symbol; the same direction must remain valid and RSI must cross the relevant threshold before a signal is created. A signal enters a four-completed-bar cooldown. State is persisted in PostgreSQL. MFE, MAE and completed bars held are persisted per signal and calculated only from candles after the signal candle; the signal candle's pre-entry range is excluded. The state machine is implemented in app/engine.py and app/mod_normal.py.

## Universe and option selection — current behaviour

The bot uses an exactly 150-entry static universe in app/symbols.py: 144 individual-security underlyings and six explicitly named NSE index underlyings. The equity list is pinned from the NSE official derivatives-underlying list checked on 19 Nov 2026. No runtime discovery, symbol rotation or automatic replacement is performed. Every configured entry is checked against the Upstox instrument master; unavailable entries are reported in /symbols and /status and are not silently replaced. The configured count and duplicate check are enforced at startup.

The option resolver selects the nearest eligible expiry and closest strike to the underlying spot for the required CE or PE. OPTION_QUOTE controls optional LTP display and defaults to off. The optional full-quote snapshot can attach bid/ask, spread, volume, OI and quote age to alerts. OPTION_LIQUIDITY_CHECKS=1 enables a fail-closed validation gate; it is disabled by default until the real Analytics Token has been verified. The Upstox V3 WebSocket feed is started for validated underlying instruments and selected option contracts are added lazily to its bounded subscription set. Fresh V3 option quotes are used when present; optional REST full quotes are a fallback controlled by OPTION_MARKET_SNAPSHOT / OPTION_LIQUIDITY_CHECKS.

## Telegram commands

Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. The polling worker only responds to the configured chat ID.

- /start and /help
- /status
- /signals
- /stats
- /pause
- /resume

Pause suppresses new signal decisions while existing signal outcome tracking continues. Polling offset and pause state are stored in the bot_state table. /stats includes average MFE/MAE for tracked signals. No command can execute a trade.

## API

- GET / and GET /health — immediate public liveness.
- GET /status — admin-authenticated scanner, Upstox, database and feed status.
- GET /diag and GET /diagnostics — admin-authenticated scanner diagnostics.
- GET /signals?limit=20 — admin-authenticated recent signals.
- GET /stats?days=30 — admin-authenticated performance summary, including average MFE/MAE when available.
- GET /symbols — currently resolved universe.
- GET /config — safe, non-secret runtime configuration.
- POST /pause and POST /resume — admin-authenticated pause controls.
- GET /force?module=normal — admin-authenticated forced evaluation.
- GET /test-telegram — admin-authenticated Telegram test.

Use Authorization: Bearer ADMIN_TOKEN. The legacy query-token form remains for compatibility but should not be used. Credentials are not returned by /config.

## Environment

Required production secrets:
- UPSTOX_ACCESS_TOKEN
- NEON_DATABASE_URL (use the SSL-enabled Neon connection string)
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- ADMIN_TOKEN

Runtime controls:
- PYTHON_VERSION: 3.11.9 in Render
- TZ: Asia/Kolkata
- AUTOSTART: 1
- RUN_SCHEDULER: 1
- UPSTOX_STREAM_ENABLED: 1 (Upstox V3 underlying/selected-option feed)
- REQUIRE_PERSISTENT_DB: 1
- KEEPALIVE: 1
- N_ADX_MIN: 22 (hard floor)
- N_SL_ATR: 1.2
- N_TP1_R: 1.5
- N_TP2_R: 2.0
- FETCH_DELAY_SEC: 6
- STALE_MAX_BARS: 1
- WORKERS: 6
- OPTION_QUOTE: 0
- OPTION_MARKET_SNAPSHOT: 0 (attach full option quote data)
- OPTION_LIQUIDITY_CHECKS: 0 (opt-in fail-closed option liquidity gate)
- OPTION_MIN_VOLUME: 100
- OPTION_MIN_OI: 500
- OPTION_MAX_SPREAD_PCT: 3.0
- OPTION_MAX_QUOTE_AGE_SEC: 20
- DRY_RUN: 0

Strategy parameters remain frozen unless explicitly approved.

## Local setup

1. Use Python 3.11.
2. Install dependencies with pip install -r requirements.txt.
3. Copy .env.example to .env and configure the secrets.
4. Run tests with AUTOSTART=0 python -m pytest -q.
5. Run locally with uvicorn app.web:app --host 0.0.0.0 --port 8000 and a configured database/secrets.

## Render

The Blueprint defines one web service and does not provision Render Postgres. Set NEON_DATABASE_URL, UPSTOX_ACCESS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and ADMIN_TOKEN in Render.

Start command:

    gunicorn app.web:app --worker-class uvicorn_worker.UvicornWorker --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT

Keep one worker. Multiple workers would create duplicate in-process schedulers and Telegram pollers. /health is the Render liveness check. Initialization fails closed if REQUIRE_PERSISTENT_DB=1 and Neon is not configured.

## Tests and known gaps

GitHub Actions runs pytest on Python 3.11. The suite covers existing indicator, state-machine, ADX, database, Upstox reliability, alert outbox and tracking behaviour, plus FastAPI routes, Telegram polling state, protobuf quote decoding, optional option-liquidity checks and no-look-ahead MFE/MAE tracking. Do not treat the bot as live-ready until CI passes and paper/live-data smoke tests verify Upstox authorization, WebSocket connection, Neon persistence and Telegram polling with real credentials.

Still not complete against the full master prompt:
1. Exactly 150 fixed static underlyings and startup count validation.
2. Wiring the option-chain endpoint into contract selection and validating the optional liquidity gate with the real Analytics Token; the full-quote REST gate is implemented but opt-in.
3. Subscribing selected option contracts to the WebSocket option-chain mode and using that stream as a quote source.
4. Expanded telemetry/configuration/feedback persistence beyond the current signals, setup-state, migration and command-state tables.
5. Time-stop and expanded performance analytics; MFE/MAE tracking is implemented.
6. No-look-ahead walk-forward backtesting.
7. The master prompt's 15-minute DI informational-only requirement conflicts with the current strategy lock because the existing 5-minute DI direction is a hard gate. No strategy change has been made.

No automatic order execution is implemented. No strategy thresholds are automatically tuned or deployed.
