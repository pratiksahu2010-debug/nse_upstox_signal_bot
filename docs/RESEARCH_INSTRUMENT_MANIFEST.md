# Research instrument manifest

Build the historical-research CSV from the exact same Upstox instrument-master
resolver used by the live scanner:

```bash
python -m scripts.build_research_manifest --output research_instruments.csv
```

The command downloads the current Upstox NSE instrument master and validates
the complete configured universe before writing anything. It fails closed if
any configured stock/index is missing, if a key is duplicated, or if the
resolved count differs from the fixed 150-symbol universe.

The output is a validated point-in-time snapshot and contains extra audit columns:

- Stocks use the same `NSE_EQ` cash-equity candle key as the live scanner.
- Indices use the same `NSE_FO` index-futures candle key as the live scanner.
- Index future expiry and price-source type are retained in the manifest.
- **The index key is not a continuous historical series.** It identifies one
  currently resolved contract and must not be reused across expiry boundaries.
- The manifest follows the configured symbol order and is atomically replaced.

Download after building the manifest:

```bash
export UPSTOX_ANALYTICS_TOKEN='...'
python -m scripts.download_historical_5m \
  --instruments research_instruments.csv \
  --from-date 2026-05-01 \
  --to-date 2026-10-31 \
  --output-dir research_data
```

The manifest is a point-in-time snapshot. Rebuild it when the Upstox master
changes, and preserve the generated CSV alongside each research dataset so the
instrument-key mapping is reproducible. The script does not alter the live
scanner, database, Telegram delivery, strategy thresholds, or order behavior.


## Why `--stocks-only` is required

The downloader now fails closed if it sees an `is_index=true` row without
`--stocks-only`. With that flag, it explicitly excludes those index-futures
rows and logs their symbols; stock cash-equity rows are still downloaded.
This prevents a current near-month futures key from being treated as six months
of continuous index history.

Correct index history requires expiry-by-expiry contract discovery, downloading
each expired contract over its actual trading life, and stitching a documented
continuous series at the roll boundary. Upstox's expired-instrument discovery
and expired-candle endpoints are Plus-plan-only. Until that integration is
implemented and validated, index-futures results must remain excluded from
historical performance statistics. Do not substitute spot index candles or
silently roll contracts.
