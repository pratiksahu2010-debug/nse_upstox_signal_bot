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

The output is directly compatible with
`scripts.download_historical_5m.py` and contains extra audit columns:

- Stocks use the same `NSE_EQ` cash-equity candle key as the live scanner.
- Indices use the same `NSE_FO` index-futures candle key as the live scanner.
- Index future expiry and price-source type are retained in the manifest.
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
