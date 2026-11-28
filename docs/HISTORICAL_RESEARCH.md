# Historical 5-minute research data

The downloader in `scripts/download_historical_5m.py` is an offline utility.
It reuses the repository's authenticated, rate-limited Upstox V3 REST client
and requests **5-minute candles only**. It is not imported by the live scanner,
does not write to PostgreSQL, does not send Telegram messages, and cannot place
orders.

## Prepare the instrument list

Create a CSV with the exact Upstox instrument keys whose candles should be
replayed:

```csv
symbol,instrument_key
RELIANCE,NSE_EQ|INE002A01018
SBIN,NSE_EQ|INE062A01020
```

Use the same underlying instrument keys consumed by the live signal engine.
For stocks, that generally means the NSE cash-equity key; for index underlyings,
use the exact index-futures key used by the live scanner. Do not substitute
option-contract keys when testing underlying-based Normal signals.

## Download

Set a read-only Upstox Analytics Token in the shell (do not put it in the CSV,
command line, source code, or logs):

```bash
export UPSTOX_ANALYTICS_TOKEN='...'
python -m scripts.download_historical_5m \
  --instruments instruments.csv \
  --from-date 2026-05-01 \
  --to-date 2026-10-31 \
  --output-dir research_data
```

The date range is inclusive. Requests are split into calendar-month chunks,
and each symbol is written to its own CSV only after all chunks for that symbol
have succeeded. Timestamps are normalized to Asia/Kolkata, duplicate candle
timestamps are collapsed, and the existing Upstox candle parser rejects
malformed/impossible OHLCV rows. An empty response is saved as a header-only
CSV and should be treated as missing data during research.

## Research limitations

- This collects underlying 5-minute OHLCV, not historical option-chain or
  option-premium quotes. Underlying R-multiples do not establish CALL/PUT
  premium profitability.
- Historical instrument keys must represent the same underlying price series
  used by the live engine. Futures rolls require an explicit contract/roll
  policy; do not stitch different contracts without documenting adjustments.
- A successful download is not a validated backtest. Replay must still enforce
  completed-candle timing, indicator warm-up, session boundaries, cooldown,
  option selection assumptions, and conservative stop/target ordering.
- Keep this dataset outside the live scanner and production database. Never
  commit access tokens or private market-data exports to the repository.


## Dataset provenance manifest

After every requested instrument has downloaded successfully, the downloader
atomically writes `dataset_manifest.json` into the output directory.
It records the requested date range, 5-minute interval, Asia/Kolkata timezone,
instrument symbol and exact instrument key, CSV filename, candle count, first
and last timestamps, download status, and SHA-256 checksum of each CSV.

`NO_DATA` entries are deliberately recorded rather than hidden. A
provenance manifest does **not** certify data quality or complete session
coverage; run the data-quality audit and require `PASS` for every
symbol before replay. Keep the manifest with the downloaded CSVs so future
research can verify that it used the same files and instrument mapping.
