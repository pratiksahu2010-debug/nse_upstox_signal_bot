# Offline Normal 5-minute replay

After downloading candles with `scripts.download_historical_5m`, audit their
coverage and completeness before replay. The replay can enforce the audit
summary as a fail-closed gate:

```bash
python -m scripts.audit_historical_5m \
  --input-dir research_data \
  --output-dir research_results/data_quality \
  --from-date 2026-05-01 \
  --to-date 2026-10-31 \
  --calendar nse_trading_dates.csv

python -m scripts.replay_historical_5m \
  --input-dir research_data \
  --output research_results/normal_5m_replay.csv \
  --ticks-csv research_ticks.csv \
  --quality-summary research_results/data_quality/symbol_summary.csv
```

When `--quality-summary` is supplied, replay stops before loading OHLCV if
the report is missing, has duplicate or mismatched symbols, or any input
symbol is not marked `PASS`. The report must be generated from the same input
directory and intended date range. For detection of fully missing sessions,
supply an authoritative NSE trading-date calendar to the audit; without one,
the audit can only assess dates that contain at least one candle. The quality
gate is optional for backward compatibility, so omit it only when you have
independently reviewed the audit results.

For a statistically cleaner evaluation window, use `--evaluation-from-date`. By default the replay then requires at least `ENGINE.warm_sessions` (currently six) distinct earlier sessions for every symbol. Those earlier candles remain available to initialize the continuous EMA/RSI/ADX/ATR series, but signals before the evaluation date are excluded from the output. Override the requirement explicitly with `--warmup-sessions N` only when the research methodology justifies it. This is a count of distinct dates in the file; the separate quality audit should still be used to verify that each session is complete.

Example: add `--evaluation-from-date 2026-05-11` to the replay command after downloading at least six prior trading sessions for every symbol.

The replay is offline and signal-only. It uses the existing Normal strategy
module, 5-minute indicators, score gate, tick-rounded stop/targets, session
window, persisted-state transition helpers and outcome simulator. For each
bar, feature generation receives only the prefix ending at that completed
bar; later candles are not passed into entry evaluation. Lifecycle state is
reset at each trading-day boundary, and the first eligible bar of each day is
used as the live engine's startup baseline rather than as an entry candidate.

The output contains one row per generated Normal signal with underlying entry,
stop, targets, score, and underlying-price outcome in R. It is not a CALL/PUT
premium backtest and does not model option selection, premium decay, spread,
slippage, fees, or execution fills. Do not interpret underlying R as option
profitability.

## Important research controls

- Download enough prior sessions to warm up the existing EMA/RSI/ADX/ATR
  calculations before the first evaluation date; discard early warm-up days
  from reported statistics.
- Prefer an explicit `--ticks-csv` file with `symbol,tick_size`
  columns. It must contain exactly one positive finite tick-size mapping for
  every input symbol; missing or duplicate mappings stop the replay. Confirm
  tick sizes from the applicable exchange/instrument reference for the
  historical period. The legacy `--tick` option remains as a fallback when
  no per-symbol CSV is supplied, but it applies one value to every symbol.
- Use continuous, correctly rolled futures history for index underlyings.
  Do not silently join expiries or use current contracts for historical dates.
- Inspect missing sessions, duplicate bars, and data coverage before comparing
  symbols. The loader rejects malformed OHLCV and unaligned 5-minute timestamps.
- The output is research-only. It does not mutate strategy configuration,
  production database state, Telegram, or broker orders.

Example `research_ticks.csv`:

```csv
symbol,tick_size
RELIANCE,0.05
ABC,0.01
```

The example values are illustrative only; do not copy them without checking
the correct tick size for each instrument and research period.

## Summarize replay results

After replay, generate an overall and per-symbol report:

```bash
python -m scripts.summarize_normal_5m_replay \
  --input research_results/normal_5m_replay.csv \
  --output research_results/normal_5m_summary.csv
```

The report includes signal/closed/open counts, win rate, average and total
net R, profit factor, and maximum drawdown in R. OPEN signals are excluded
from closed-trade performance metrics. The combined ALL row sums signal R
chronologically; it is a descriptive research series, not a capital-weighted
portfolio backtest and does not model overlapping exposure or position sizing.
All R statistics are based on the underlying price path and the existing
conservative OHLC outcome simulator. They are not CALL/PUT option-premium
returns and exclude option selection, decay, spreads, slippage, fees, and fills.
