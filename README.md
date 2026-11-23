# Normal NSE F&O CALL/PUT Signal Bot

Signal-only NSE F&O application. It does not place, modify or execute broker orders.

## Runtime

- Python 3.11 and FastAPI.
- Gunicorn with Uvicorn worker; exactly one worker because the scanner is in-process.
- Upstox REST remains the source of completed 5-minute OHLCV candles and instrument master data.
- Upstox Market Data Feed V3 WebSocket provides live quote/feed-health observations. Live ticks do not create signals.
- Neon PostgreSQL is the production persistent store. NEON_DATABASE_URL is preferred; DATABASE_URL remains a compatibility alias. The database bootstrap is tracked through an idempotent schema_migrations version table.
- Telegram Bot API getUpdates long polling; no webhook. Missing or rejected Upstox credentials trigger throttled Telegram alerts; after replacing a Render secret, restart the service to reconnect with the new token.

## Strategy

One production signal module, Normal only, runs on the fixed 150-symbol universe and completed 5-minute candle data.

**Normal CALL setup:** completed 5m EMA9 > EMA21, close > session VWAP, ADX14 >= 20, then RSI14 crosses upward through 52.

**Normal PUT setup:** completed 5m EMA9 < EMA21, close < session VWAP, ADX14 >= 20, then RSI14 crosses downward through 42.

No 15m candles or higher-timeframe context are used in signal generation. ADX uses Wilder smoothing and N_ADX_MIN is hard-floored at 20. The 0–100 Normal score grades 5m EMA9/21 spread, 5m EMA21 slope, VWAP distance, ADX and RSI; DI, RVOL and candle-shape factors are not signal gates or score components.

Normal assigns a deterministic 0–100 quality score and applies one final score gate, N_SCORE_MIN (default 72). No separate OI or extra score gates are added.

**Momentum Explosion is disabled in production.** The bot emits Normal signals only; the code hard-disables Momentum so a stale Render environment variable cannot re-enable it. Historical Momentum rows remain available for outcome/history compatibility.

## Risk and state

Default underlying reference levels:
- Stop loss: 1.2 × 5-minute ATR(14).
- TP1: 1.5R.
- TP2: 2.0R.

The first completed candle observed after startup is a baseline. A qualifying setup can arm a symbol; the same direction must remain valid and RSI must cross the relevant threshold before a signal is created. A signal enters a four-completed-bar cooldown. State is persisted in PostgreSQL. MFE, MAE and completed bars held are persisted per signal and calculated only from candles after the signal candle; the signal candle's pre-entry range is excluded. The state machine is implemented in app/engine.py and app/mod_normal.py.

## Universe and option selection — current behaviour

The bot uses an exactly 150-entry static universe in app/symbols.py: 144 selected NSE F&O stock underlyings and six explicitly named NSE index underlyings. IEX and IREDA are excluded ahead of their 25 Nov 2026 F&O exit under NSE circular NSE/FAOP/76497; RELIANCE and SBIN are included as liquid replacements. No runtime discovery, symbol rotation or silent substitution is performed. Every configured entry is checked against the Upstox instrument master; unavailable entries are reported in /symbols and /status. An incomplete universe remains fail-closed but is retried with a five-minute backoff.

The option resolver selects the nearest eligible expiry and closest strike to the underlying spot for the required CE or PE. OPTION_QUOTE controls optional LTP display and defaults to off. Full quote snapshots remain available for display/tracking; strict liquidity validation is disabled by default. When OPTION_LIQUIDITY_CHECKS=1, a contract must pass fresh bid/ask depth, spread, volume, OI and quote-age checks; if no candidate passes, no option recommendation or signal alert is emitted. There is no unverified ATM fallback when the liquidity gate is enabled. Verify optional quote access during the credentialed smoke test; when strict liquidity validation is disabled, quote failure does not block the underlying signal. The Upstox V3 WebSocket feed is started for validated underlying instruments and selected option contracts are added lazily to its bounded subscription set.

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

- GET / and GET /health — immediate public liveness; they do not imply the bot is ready to scan.
- GET /ready — Render readiness probe; returns 200 only after required Upstox/Telegram configuration, scheduler, persistent database connectivity and the full fixed universe are valid. During the entry window it also requires a recent successful scan with at least 90% of configured symbols ending on the exact latest eligible completed 5m candle. A non-empty but stale dataframe does not count as fresh coverage. An active Telegram polling leader with a persistent getUpdates 409 conflict fails readiness. During market hours it requires a connected V3 feed worker, all 150 underlying subscriptions, and an actual accepted market quote within 120 seconds (empty WebSocket frames do not count as fresh quotes).
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

The committed `.env.example` is a **safe template**, not a production environment dump. Copy it to `.env` for local use and enter secrets locally. Never commit a real `.env`, access token, Telegram token, Neon connection string, or admin token. The production values belong in Render Dashboard → **Environment**.

### Required Render secrets

Set all five variables below. The application fails readiness when required configuration is missing or invalid.

- `UPSTOX_ACCESS_TOKEN` — Upstox Analytics/API access token with the required market-data permissions.
- `NEON_DATABASE_URL` — Neon PostgreSQL connection string; use the SSL-enabled URL.
- `TELEGRAM_BOT_TOKEN` — Telegram bot token.
- `TELEGRAM_CHAT_ID` — destination chat ID allowed by the bot.
- `ADMIN_TOKEN` — long, random secret used by admin-authenticated endpoints.

`NEON_DATABASE_URL` takes precedence. `DATABASE_URL` is accepted only as a compatibility fallback. Do not set both to different databases.

### Runtime configuration reference

The values below match the committed `.env.example` template and the defaults in `app/config.py`. Render's Dashboard values override the template. Candle fetching uses at most one scanner refresh pass in addition to bounded per-request retries. The strategy defaults in this PR are documented for review; no Render environment settings are changed by this PR.

| Variable | Template/default | Purpose |
|---|---:|---|
| `TZ` | `Asia/Kolkata` | Process/log timezone; strategy timestamps use IST |
| `AUTOSTART` | `1` | Initialize the application on startup |
| `RUN_SCHEDULER` | `1` | Enable scheduled scans |
| `UPSTOX_STREAM_ENABLED` | `1` | Enable Upstox V3 market-data feed |
| `REQUIRE_PERSISTENT_DB` | `1` | Fail closed without persistent PostgreSQL |
| `KEEPALIVE` | `1` | Render keepalive self-ping when URL is available |
| `LOG_LEVEL` | `INFO` | Application log level |
| `ENABLE_NORMAL` | `1` | Enable the Normal signal module |
| `ENABLE_MOMENTUM` | `0` | Retired; Momentum is hard-disabled in production |
| `DRY_RUN` | `0` | When 1, log signals without Telegram delivery |
| `FETCH_DELAY_SEC` | `6` | Delay after candle close before fetching |
| `STALE_MAX_BARS` | `1` | Legacy compatibility setting; new entries require the exact latest eligible 5m candle |
| `WORKERS` | `24` (bounded to 16–24) | I/O-bound candle fetch concurrency; Upstox request pacing remains enforced |
| `RATE_PER_SEC` | `15` | Upstox request pacing per second |
| `RATE_PER_MIN` | `450` | Upstox request pacing per minute |
| `RATE_PER_30MIN` | `1800` | Upstox request pacing per 30 minutes |
| `PREWARM_NSE` | `08:40` | IST prewarm time |
| `HEARTBEAT_MIN` | `60` | Diagnostics heartbeat interval; 0 disables |
| `EOD_SUMMARY` | `1` | Enable end-of-day summary |
| `N_FIRST_SIGNAL` | `09:20` | Earliest signal time (IST) |
| `N_LAST_ENTRY` | `15:00` | Last entry time (IST) |
| `N_SESSION_END` | `15:15` | Strategy session end (IST) |
| `N_ADX_MIN` | `20` | ADX14 minimum; code enforces a hard floor of 20 |
| `N_SCORE_MIN` | `72` | Single final Normal quality-score gate |
| `N_SL_ATR` | `1.2` | Stop distance in ATR multiples |
| `N_TP1_R` | `1.5` | First target in R |
| `N_TP2_R` | `2.0` | Second target in R |
| `N_DAILY_CAP` | `1000` | Daily signal cap |
| `N_PER_CYCLE_TOP` | `1000` | Maximum candidates per scan cycle |
| `M_FIRST_SIGNAL` | `09:20` | Earliest Momentum entry time (IST) |
| `M_LAST_ENTRY` | `15:00` | Last Momentum entry time (IST) |
| `M_SESSION_END` | `15:15` | Momentum outcome-tracking session end (IST) |
| `M_SCORE_MIN` | `78` | Single final Momentum quality-score gate |
| `M_COOLDOWN_BARS` | `2` | Same-direction Momentum signal cooldown |
| `M_DAILY_CAP` | `1000` | Daily Momentum signal cap |
| `M_PER_CYCLE_TOP` | `1000` | Maximum Momentum candidates per scan cycle |
| `OPTION_MIN_DAYS` | `1` | Minimum days to option expiry |
| `OPTION_QUOTE` | `0` | Optional option LTP display |
| `OPTION_MARKET_SNAPSHOT` | `1` | Fetch optional full quote snapshots for display/tracking |
| `OPTION_LIQUIDITY_CHECKS` | `0` | Optional strict liquidity gate; disabled by default as approved |
| `OPTION_MIN_VOLUME` | `100` | Minimum option volume when checks enabled |
| `OPTION_MIN_OI` | `500` | Minimum option open interest when checks enabled |
| `OPTION_MAX_SPREAD_PCT` | `3.0` | Maximum spread percentage when checks enabled |
| `OPTION_MAX_QUOTE_AGE_SEC` | `20` | Maximum option quote age |
| `OPTION_PREMIUM_TRACKING` | `1` | Descriptive option-premium tracking; does not gate signals |
| `TRAINING_COST_R` | `0.0` | Configured round-trip cost in underlying R units |
| `OPTION_EST_ROUND_TRIP_FEE_PCT` | `0.0` | User-supplied estimated round-trip fee percentage |
| `OPTION_EST_SLIPPAGE_BPS_PER_SIDE` | `0.0` | User-supplied estimated slippage per side in basis points |

Option quote snapshots remain available for display/tracking; strict liquidity validation defaults to off. If the production Analytics Token cannot retrieve fresh depth/volume/OI while the gate is off, underlying signals continue and option quote fields remain unavailable. An explicit Render environment override can change these defaults, so inspect the variable names (not secret values) before merging. Cost estimates default to zero and are not actual broker charges.

### Scanner leadership during rolling deploys

Every scan boundary acquires a PostgreSQL-backed, owner-token lease before fetching the 150-symbol universe. The lease is renewed every 20 seconds while a scan runs and expires after 120 seconds if the process dies. A second Render instance or `/force` request cannot start a competing scan while another process owns the lease. Acquisition fails closed when PostgreSQL is unavailable; a process that detects lease loss stops evaluating and suppresses signal dispatch. This complements the in-process mutex and the unique signal key; it does not replace the required single Gunicorn worker.

### Render-specific settings

The Blueprint sets Python 3.11.9, `TZ=Asia/Kolkata`, `KEEPALIVE=1` and `REQUIRE_PERSISTENT_DB=1`; secret variables are configured in the Render Dashboard. The deployed service must use one Gunicorn worker because the scanner and Telegram poller are in-process. The internal `WORKERS` setting controls fetch concurrency (bounded to 16–24) and is unrelated to Gunicorn's `--workers 1`.

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

Keep one worker. Multiple workers would create duplicate in-process schedulers and Telegram pollers. /health is the Render deployment liveness check; /ready remains the strict operational-readiness check. The readiness check also verifies recent scan completion, Upstox REST freshness and live-feed freshness during market hours. Set the Render Dashboard Settings → Health Check Path to `/health` (the Blueprint uses `/health`). This lightweight liveness probe prevents a rolling deployment from waiting for the first 150-symbol cold-start scan or a live market quote. Keep `/ready` as the strict operational-readiness endpoint: it verifies persistent storage, credentials, the resolved universe, recent scanner freshness, option-quote access and the live feed during market hours. A `healthCheckPath` change in this repository does not retroactively change a manually created Render service that is not managed by a Blueprint; update the existing service's Dashboard setting as well.

## Tests and known gaps

GitHub Actions runs pytest on Python 3.11. The suite covers existing indicator, state-machine, ADX, database, Upstox reliability, alert outbox and tracking behaviour, plus FastAPI routes, Telegram polling state, protobuf quote decoding, optional option-liquidity checks and no-look-ahead MFE/MAE tracking. Do not treat the bot as live-ready until CI passes and paper/live-data smoke tests verify Upstox authorization, WebSocket connection, Neon persistence and Telegram polling with real credentials.

Remaining master-prompt work and live-verification gaps:
1. The exact 150-symbol static list and startup validation are implemented. It contains 144 selected NSE F&O stocks plus six explicitly allowlisted index underlyings. IEX and IREDA are excluded ahead of their 25 Nov 2026 F&O exit and RELIANCE and SBIN replace them. Startup remains fail-closed if the configured list is not exactly 150 unique entries or if all 150 cannot be validated against the Upstox instrument master; an incomplete master resolution is retried after a five-minute backoff.
2. Option contracts are resolved from the daily instrument master. Upstox option-contract and option-chain REST methods exist, but option-chain OI/PCR context is not yet wired into selection. The full-quote liquidity gate defaults to enabled and must be verified with the production Analytics Token before merging; no unverified fallback is emitted when the gate is active.
3. The V3 WebSocket subscribes to all validated underlyings and batches selected option-contract subscriptions; fresh option quotes are attached when available. Live feed authorization, quote freshness and liquidity thresholds still require a credentialed paper-session smoke test.
4. MFE/MAE, bars held, a persistent six-bar time-stop review flag, time-to-TP1/TP2 and option-premium outcome tracking are implemented. Live quote coverage and cost estimates still require credentialed paper-session validation. Signal feedback buttons, user labels, automatic gross/net-R labels, indicator snapshots and bounded offline walk-forward research are implemented.
5. The offline trainer searches only a small bounded set of post-signal cohort filters (ADX/RVOL), uses chronological train/validation/held-out partitions and enforces sample floors. It does not reconstruct missed signals or claim counterfactual strategy performance. Proposals are stored as diffs; /approve stages approval and /rollback marks rollback, but neither changes live strategy settings. Optional /shadow mode records whether each new live signal would match the proposal and never blocks or alters alerts. Configure TRAINING_COST_R from measured round-trip costs; its default is zero, so net-R is not cost-adjusted until configured.
6. The production signal generator is Normal-only and uses completed 5-minute candles only; no 15-minute filter is used. Option-chain OI/PCR context is not yet wired into entry selection. The historical 528-second scan-duration issue still needs a fresh live-session timing measurement. Actual Telegram delivery and option quote entitlement still need a credentialed paper-session check.

No automatic order execution is implemented. No strategy thresholds are automatically tuned or deployed. A green CI run does not certify live Upstox, Neon or Telegram connectivity; those require real credentials and a paper-session check.


### Outcome timing metrics

The offline training export includes `bars_to_tp1` and `bars_to_tp2`: completed 5-minute bars from the signal candle to the first target touch. These are descriptive outcome metrics only; they do not change live entry, exit, alert, or lifecycle rules. Same-candle stop/target ambiguity follows the existing conservative tracker rule (stop first; after TP1, breakeven first).


### Option-premium and cost reconciliation

When `OPTION_PREMIUM_TRACKING=1`, each new signal stores its selected option instrument key and a fresh signal-time quote reference. Long option entry is marked at the ask (LTP is a tagged fallback); subsequent exits are marked at the bid (LTP fallback). The bot records option-premium MFE/MAE in points and percent, last quote timestamp/age, quote sample count and source. A missing or stale entry quote is never backfilled from a later quote, so that signal remains marked `MISSING_ENTRY_QUOTE` rather than receiving a biased premium excursion.

The quote-based premium result includes the bid/ask spread naturally. Optional fee/slippage estimates are controlled by `OPTION_EST_ROUND_TRIP_FEE_PCT` and `OPTION_EST_SLIPPAGE_BPS_PER_SIDE`; both default to zero and are explicitly estimates, not Upstox tariff calculations. Configure them from your own current broker cost schedule before using estimated net premium returns. The existing underlying-R training cost setting is separate.

Because this bot is signal-only and never places orders, it cannot know actual fills, actual brokerage or actual slippage automatically. To reconcile a real or paper execution from your broker record, send `/actualcost <signal_id> <quantity_units> <brokerage_rupees> <other_fees_rupees> <slippage_rupees>`. The command stores brokerage, other statutory/transaction fees and slippage separately and records cost per option unit (quantity is entered in units, not lots); it does not invent execution prices or alter signal outcomes. These actual cost fields are included in `/export`. Historical signals created before option contract keys were retained cannot be backfilled with reliable option-premium history.
