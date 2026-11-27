# Offline Normal 5-minute replay

After downloading candles with `scripts.download_historical_5m`, replay them
without connecting to the live scanner:

```bash
python -m scripts.replay_historical_5m \
  --input-dir research_data \
  --output research_results/normal_5m_replay.csv \
  --ticks-csv research_ticks.csv
```

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
