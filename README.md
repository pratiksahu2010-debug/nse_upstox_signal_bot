# NSE Midcap + Index CALL/PUT Signal Bot

A single NSE signal engine using Upstox market data, Render, PostgreSQL and Telegram. **Signals only**: it never places orders.

## Universe
- One midcap-oriented basket of up to 100 NSE F&O stock candidates from `app/symbols.py`.
- Each stock is accepted only when the daily Upstox master contains its NSE cash EQ, futures, and both CE and PE option contracts on a usable common expiry.
- All NSE index underlyings with `underlying_type=INDEX` futures and both CE/PE options are discovered dynamically from the same daily master. There is no fixed five-index limit.
- The status endpoint reports resolved stock count, index count and dropped symbols. The bot never silently substitutes a large-cap stock for an invalid candidate.

## One strategy only
Signal decisions use the latest fully closed 5-minute candle:
- CALL: EMA9 > EMA21, close > VWAP, RSI14 > 50, ADX14 >= 18, +DI > -DI, and bullish completed 15-minute EMA9/EMA21 context.
- PUT: EMA9 < EMA21, close < VWAP, RSI14 < 50, ADX14 >= 18, -DI > +DI, and bearish completed 15-minute EMA9/EMA21 context.

No ORB, Momentum Explosion, MCX, score threshold, RVOL gate, candle-quality gate, EMA-cross trigger, pullback trigger or NIFTY market-bias veto.

## Stop and targets
Default levels are calculated from the signal candle close:
- Stop loss: 1.2 × 5-minute ATR(14)
- Target 1: 1.5R
- Target 2: 2.0R

These are defaults and can be adjusted with `N_SL_ATR`, `N_TP1_R` and `N_TP2_R`. They are reference levels, not a guarantee of execution price or outcome.

## Alerts and safeguards
- Telegram displays CALL (CE) or PUT (PE), signal candle close time in IST, entry, ATR stop, targets, and the actual available option contract.
- A signal is not sent if its CE/PE contract cannot be resolved at alert time.
- Closed-candle processing, stale-data rejection, duplicate protection, a short same-direction cooldown, persistent signal tracking and Telegram outbox retries remain enabled.
- Manual execution only. No broker order API is called.

## Render setup
Set `UPSTOX_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL` and `ADMIN_TOKEN`. Keep one Gunicorn worker because the scheduler runs inside the process. Health endpoint: `/health`. Admin endpoints `/status`, `/diag`, `/stats`, `/force?module=all` require the bearer admin token.

## Local tests
`pip install -r requirements.txt pytest && AUTOSTART=0 python -m pytest -q`

No threshold guarantees profitable trades. Validate signals on historical and live paper data before considering real-money execution.
