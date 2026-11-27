"""Download read-only 5-minute Upstox candles for offline research.

Input CSV columns: symbol,instrument_key
Example:
    python -m scripts.download_historical_5m \
        --instruments instruments.csv --from-date 2026-05-01 \
        --to-date 2026-10-31 --output-dir research_data

This script is deliberately separate from the live scanner. It never sends
orders, changes strategy settings, or writes to the production database.
"""
import argparse
import csv
import logging
import os
import re
import sys
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from app.upstox import UpstoxClient, UpstoxError

log = logging.getLogger("historical_5m")
COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]


def monthly_chunks(start: date, end: date):
    """Yield inclusive calendar-month slices, clipped to the requested range."""
    if start > end:
        raise ValueError("from-date must be on or before to-date")
    cursor = start
    while cursor <= end:
        month_end = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
        chunk_end = min(month_end, end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def read_instruments(path: Path):
    """Read and validate an explicit symbol-to-Upstox-instrument-key CSV."""
    instruments = []
    seen_symbols = set()
    seen_keys = set()
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"symbol", "instrument_key"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError("instrument CSV must contain symbol,instrument_key columns")
        for line, row in enumerate(reader, start=2):
            symbol = (row.get("symbol") or "").strip().upper()
            key = (row.get("instrument_key") or "").strip()
            if not symbol or not key:
                raise ValueError(f"missing symbol or instrument_key on CSV line {line}")
            if symbol in seen_symbols or key in seen_keys:
                raise ValueError(f"duplicate symbol or instrument_key on CSV line {line}")
            if not re.fullmatch(r"[A-Z0-9_.&-]+", symbol):
                raise ValueError(f"invalid symbol on CSV line {line}")
            seen_symbols.add(symbol)
            seen_keys.add(key)
            instruments.append((symbol, key))
    if not instruments:
        raise ValueError("instrument CSV contains no instruments")
    return instruments


def download_one(client: UpstoxClient, instrument_key: str, start: date, end: date):
    """Fetch and combine monthly 5-minute chunks; return a clean IST-indexed frame."""
    frames = []
    for chunk_start, chunk_end in monthly_chunks(start, end):
        frame = client.historical(instrument_key, chunk_start, chunk_end, interval=5)
        if frame is not None and not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
    result = pd.concat(frames).sort_index()
    result = result[~result.index.duplicated(keep="last")]
    result = result.loc[(result.index.date >= start) & (result.index.date <= end)]
    return result


def write_atomic(frame: pd.DataFrame, destination: Path):
    """Replace a symbol CSV only after all requested monthly chunks succeeded."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".tmp")
    output = frame.copy()
    output.index.name = "timestamp"
    output.to_csv(temp, columns=["open", "high", "low", "close", "volume", "oi"])
    os.replace(temp, destination)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", required=True, type=Path,
                        help="CSV containing symbol,instrument_key columns")
    parser.add_argument("--from-date", required=True, type=date.fromisoformat,
                        help="inclusive YYYY-MM-DD")
    parser.add_argument("--to-date", required=True, type=date.fromisoformat,
                        help="inclusive YYYY-MM-DD")
    parser.add_argument("--output-dir", type=Path, default=Path("research_data"))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.from_date > args.to_date:
        raise SystemExit("--from-date must be on or before --to-date")
    if not (os.getenv("UPSTOX_ANALYTICS_TOKEN") or os.getenv("UPSTOX_ACCESS_TOKEN")):
        raise SystemExit("Set UPSTOX_ANALYTICS_TOKEN (or legacy UPSTOX_ACCESS_TOKEN) first")

    try:
        instruments = read_instruments(args.instruments)
        client = UpstoxClient()
        for symbol, key in instruments:
            log.info("Downloading 5-minute history for %s", symbol)
            frame = download_one(client, key, args.from_date, args.to_date)
            target = args.output_dir / f"{symbol}.csv"
            write_atomic(frame, target)
            log.info("%s: %d candles saved to %s", symbol, len(frame), target)
    except (OSError, ValueError, UpstoxError) as exc:
        log.error("Historical download stopped: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
