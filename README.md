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

There is one active strategy module: Normal NSE F&O. The existing strategy logic in app/mod_normal.py is retained; the approved 5-minute ADX minimum is 19.

CALL setup: EMA9 > EMA21, close > session VWAP, ADX14 >= 19, +DI > -DI, followed by RSI14 crossing upward through 52.

PUT setup: EMA9 < EMA21, close < session VWAP, ADX14 >= 19, -DI > +DI, followed by RSI14 crossing downward through 42.

ADX uses Wilder smoothing. N_ADX_MIN is hard-floored at 19 and may be raised with the environment variable. The strategy uses completed 5-minute candles only; no higher-timeframe context is calculated.

Proposed higher-timeframe context and other strategy changes are not silently applied because the existing strategy was explicitly locked. The current 5-minute DI gate, RSI-cross trigger, universe resolver and ATR target parameters remain unchanged.

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
- /relabel <signal_id> <GOOD|BAD|LATE|FAKE|SKIP>
- /train — bounded offline walk-forward research
- /proposals — inspect the pending proposal and held-out metrics
- /approve <proposal_id> — stage approval; does not edit live settings
- /rollback <proposal_id> — mark a proposal rolled back
- /shadow on|off — log proposal matches on future alerts without gating them
- /export — send a recent signal/outcome CSV document
- /pause
- /resume

Pause suppresses new signal decisions while existing signal outcome tracking continues. Polling offset, pause state, feedback labels and proposal state are stored persistently. Each new signal alert includes Good / Bad / Late / Fake / Skip inline feedback buttons. Human feedback is stored separately and takes precedence over the automatic categorical label in training reports; measured net R remains the performance objective and is never fabricated from subjective feedback. SKIP labels are excluded from training. Only completed outcomes are eligible, and train/validation/held-out partitions are split by complete trading days with an eight-day minimum. The trainer uses three expanding-window validation folds followed by a final chronological holdout that is not used for candidate selection. A one-bar embargo removes samples whose tracked outcome windows reach the next partition; same-symbol overlapping outcome windows are also purged. Both purge counts and per-fold sample diagnostics are reported. Candidate and baseline reports include net-R expectancy, win rate, profit factor, average win/loss and maximum drawdown. Indicator snapshots are persisted with each signal. /stats includes average MFE/MAE for tracked signals. No command can execute a trade.

## API

- GET / and GET /health — immediate public liveness; they do not imply the bot is ready to scan.\n- GET /ready — Render readiness probe; returns 200 only after required Upstox/Telegram configuration, scheduler, persistent database connectivity and (when resolved) the full fixed universe are valid.
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
- N_ADX_MIN: 19 (hard floor)
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
- TRAINING_COST_R: 0.0 (configured round-trip cost in underlying R units; set from your measured fees/slippage before treating net-R as cost-adjusted)

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

Keep one worker. Multiple workers would create duplicate in-process schedulers and Telegram pollers. /ready is the Render readiness check; /health remains a liveness-only endpoint. The service is not marked ready when required secrets, the scheduler, Neon connectivity, Upstox authentication or the resolved fixed universe are invalid.

## Tests and known gaps

GitHub Actions runs pytest on Python 3.11. The suite covers existing indicator, state-machine, ADX, database, Upstox reliability, alert outbox and tracking behaviour, plus FastAPI routes, Telegram polling state, protobuf quote decoding, optional option-liquidity checks and no-look-ahead MFE/MAE tracking. Do not treat the bot as live-ready until CI passes and paper/live-data smoke tests verify Upstox authorization, WebSocket connection, Neon persistence and Telegram polling with real credentials.

Remaining master-prompt work and live-verification gaps:
1. The exact 150-symbol static list and startup validation are implemented. It contains 144 NSE individual-security underlyings plus six explicitly allowlisted NSE index underlyings. Startup fails closed if the configured list is not exactly 150 unique entries or if all 150 cannot be validated against the Upstox instrument master; unavailable symbols are reported, never silently replaced.
2. Option contracts are resolved from the daily instrument master. Upstox option-contract and option-chain REST methods exist, but option-chain OI/PCR context is not yet wired into selection. The full-quote liquidity gate is opt-in and must be verified with the user's live Analytics Token before enabling.
3. The V3 WebSocket subscribes to all validated underlyings and batches selected option-contract subscriptions; fresh option quotes are attached when available. Live feed authorization, quote freshness and liquidity thresholds still require a credentialed paper-session smoke test.
4. MFE/MAE, bars held, a persistent six-bar time-stop review flag and detailed signal analytics are implemented. Exact time-to-TP1/TP2 and option-premium outcome tracking remain incomplete. Signal feedback buttons, user labels, automatic gross/net-R labels, indicator snapshots and bounded offline walk-forward research are implemented.
5. The offline trainer searches only a small bounded set of post-signal cohort filters (ADX/RVOL), uses chronological train/validation/held-out partitions and enforces sample floors. It does not reconstruct missed signals or claim counterfactual strategy performance. Proposals are stored as diffs; /approve stages approval and /rollback marks rollback, but neither changes live strategy settings. Optional /shadow mode records whether each new live signal would match the proposal and never blocks or alters alerts. Configure TRAINING_COST_R from measured round-trip costs; its default is zero, so net-R is not cost-adjusted until configured.
6. The existing 5-minute +DI/-DI direction remains a hard gate because the current strategy was explicitly locked. No 15-minute context is calculated or displayed.

No automatic order execution is implemented. No strategy thresholds are automatically tuned or deployed. A green CI run does not certify live Upstox, Neon or Telegram connectivity; those require real credentials and a paper-session check.


### Outcome timing metrics

The offline training export includes `bars_to_tp1` and `bars_to_tp2`: completed 5-minute bars from the signal candle to the first target touch. These are descriptive outcome metrics only; they do not change live entry, exit, alert, or lifecycle rules. Same-candle stop/target ambiguity follows the existing conservative tracker rule (stop first; after TP1, breakeven first).


### Option-premium and cost reconciliation

When `OPTION_PREMIUM_TRACKING=1`, each new signal stores its selected option instrument key and a fresh signal-time quote reference. Long option entry is marked at the ask (LTP is a tagged fallback); subsequent exits are marked at the bid (LTP fallback). The bot records option-premium MFE/MAE in points and percent, last quote timestamp/age, quote sample count and source. A missing or stale entry quote is never backfilled from a later quote, so that signal remains marked `MISSING_ENTRY_QUOTE` rather than receiving a biased premium excursion.

The quote-based premium result includes the bid/ask spread naturally. Optional fee/slippage estimates are controlled by `OPTION_EST_ROUND_TRIP_FEE_PCT` and `OPTION_EST_SLIPPAGE_BPS_PER_SIDE`; both default to zero and are explicitly estimates, not Upstox tariff calculations. Configure them from your own current broker cost schedule before using estimated net premium returns. The existing underlying-R training cost setting is separate.

Because this bot is signal-only and never places orders, it cannot know actual fills, actual brokerage or actual slippage automatically. To reconcile a real or paper execution from your broker record, send `/actualcost <signal_id> <quantity_units> <brokerage_rupees> <other_fees_rupees> <slippage_rupees>`. The command stores brokerage, other statutory/transaction fees and slippage separately and records cost per option unit (quantity is entered in units, not lots); it does not invent execution prices or alter signal outcomes. These actual cost fields are included in `/export`. Historical signals created before option contract keys were retained cannot be backfilled with reliable option-premium history.
