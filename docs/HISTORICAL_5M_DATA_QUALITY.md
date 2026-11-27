# Historical 5-minute data-quality audit

Run this after downloading data and before replaying any signals:

```bash
python -m scripts.audit_historical_5m \
  --input-dir research_data \
  --output-dir research_results/data_quality \
  --from-date 2026-05-01 \
  --to-date 2026-10-31
```

The read-only audit writes two reports:

- `symbol_summary.csv`: one row per input symbol, with overall PASS / REVIEW /
  NO_DATA status and counts.
- `session_detail.csv`: one row per observed session (or each date in an
  explicitly supplied calendar), with complete/incomplete/no-data status,
  missing 5-minute bars, duplicates, and off-session candles.

A regular NSE session is checked against 75 candle start times from 09:15
through 15:25 IST. A session is COMPLETE only when every expected timestamp is
present exactly once and there are no off-session candles. A header-only
download is reported as NO_DATA rather than being silently treated as a valid
empty history.

## Missing whole sessions and holidays

Without a calendar, the audit checks only dates that contain at least one
candle. It deliberately does not assume every weekday is an NSE trading day,
because holidays and special sessions would create false missing-session
alarms.

To detect entire missing sessions, provide an authoritative NSE trading-date
CSV with a `date` column:

```csv
date
2026-05-04
2026-05-05
2026-05-06
```

Then run:

```bash
python -m scripts.audit_historical_5m \
  --input-dir research_data \
  --output-dir research_results/data_quality \
  --from-date 2026-05-01 \
  --to-date 2026-10-31 \
  --calendar nse_trading_dates.csv
```

Only include actual trading sessions in that calendar. The audit flags dates
with no candles as NO_DATA. It does not fetch market data, modify downloaded
files, change the Normal strategy, connect to PostgreSQL/Telegram, or place
orders. Review and resolve REVIEW / NO_DATA rows before using replay results.
