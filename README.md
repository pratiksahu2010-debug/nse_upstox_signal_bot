# NSE F&O + MCX 5-minute signal bot (Upstox · Render free · Postgres · Telegram)

Three independent modules, one process, signals only (no orders).

| Module | Universe | Idea |
|---|---|---|
| **NORMAL** | 50 liquid F&O stocks + 5 indices (via near-month index futures) | EMA9/21 + VWAP + RSI + ADX>=17 + ATR; fresh cross or pullback-resume |
| **ORB** | 100 different F&O stocks | 09:15-09:30 opening range breakout / retest, volume + gap + VWAP filters |
| **MCX** | auto-built from MCX master (all liquid futures, max 100) | Donchian-20 breakout with range expansion, ADX>=20 |

All gates and numbers are in `app/config.py` and overridable by environment variables.

## Deploy on Render (free)
1. Push this folder to a GitHub repo. Render > New > Blueprint > select repo (uses `render.yaml`: free web service + free Postgres).
2. Set secrets: `UPSTOX_ACCESS_TOKEN` (Upstox Developer Apps > **Analytics Token**, valid 1 year, read-only market data, no static IP needed), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. `ADMIN_TOKEN` is auto-generated.
3. Free web services sleep after 15 min without traffic. The bot self-pings, but also add an external pinger (UptimeRobot / cron-job.org) on `https://<app>.onrender.com/health` every 5 min.
4. Note: Render's free Postgres has a limited lifetime (check your dashboard); if it expires, create a new one and update `DATABASE_URL`. Without a DB the bot still runs (memory store) but forgets cooldowns/stats on restart.
5. Keep **one** gunicorn worker (already in `render.yaml`).

## Endpoints (`?token=ADMIN_TOKEN`)
`/health` · `/status` · `/diag` (why symbols were rejected, per module) · `/stats` (win rate, net R from tracked alerts) · `/force?module=normal|orb|mcx|all` (evaluate now, ignoring clock windows, to test the pipeline) · `/test-telegram`

## Why it is strict (false-signal rejection)
Signal candle must be CLOSED and fresh (older than 1 bar = suppressed); trend + VWAP side; ADX>=min with DI spread and trigger-aware ADX behaviour; RSI band and direction; time-of-day relative volume; conviction candle (body, close position, no exhaustion bar); no chasing (distance from VWAP/EMA9/OR level in ATR); whipsaw filter (EMA crosses in last 20 bars); 15m EMA trend; NIFTY bias for stocks; swing stop that is rejected (not widened) if wider than 1.8-2.0 ATR; score >= 65; cooldowns, daily caps and top-N per cycle.
If `/diag` shows a gate rejecting almost everything, loosen only that gate through its env var.

## Honest limits
- No threshold set guarantees profit. Run 2-3 weeks and read `/stats` before trusting it with money.
- Candles are polled over REST at each 5-minute close (not websocket). Rate budget is enforced (<=1700 calls / 30 min vs Upstox 2000).
- MCX has only ~30-40 liquid futures, so the MCX universe will be below 100; illiquid ones are auto-disabled (see `/status`).
- Symbol lists (`app/symbols.py`) are validated against the live Upstox master and backfilled from reserves; edit freely.
- Option premium in alerts is a best-effort lookup and is omitted if it fails. MCX winter close is 23:55 (set `M_MARKET_CLOSE` / `M_LAST_ENTRY` if timings differ).
- Not financial advice.

## Local test
`pip install -r requirements.txt pytest && python -m pytest -q tests` · `DRY_RUN=1 gunicorn app.web:app`
