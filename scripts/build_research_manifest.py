"""Build a complete research CSV from the exact live 150-symbol resolver.

The resulting CSV is directly consumable by download_historical_5m.py.
Stocks use the live cash-equity candle key; indices use the same index-futures
key used by the live scanner. The script fails closed on any partial universe.
"""
import argparse
import csv
import os
import sys
from pathlib import Path

from app import symbols
from app.universe import build_universe

FIELDS = [
    "symbol",
    "instrument_key",
    "price_source",
    "segment",
    "instrument_type",
    "expiry",
    "is_index",
]


def manifest_rows(universe):
    """Validate and serialize the exact configured universe in stable order."""
    if not universe.ready:
        raise ValueError(
            "live universe is not ready: " + "; ".join(universe.validation_errors)
        )

    by_symbol = {}
    for item in universe.normal:
        if item.symbol in by_symbol:
            raise ValueError(f"duplicate resolved symbol: {item.symbol}")
        by_symbol[item.symbol] = item

    expected = list(symbols.FIXED_FNO_SYMBOLS)
    missing = sorted(set(expected) - set(by_symbol))
    unexpected = sorted(set(by_symbol) - set(expected))
    if len(by_symbol) != symbols.EXPECTED_UNIVERSE_SIZE or missing or unexpected:
        raise ValueError(
            f"resolved universe mismatch: count={len(by_symbol)}, "
            f"missing={missing}, unexpected={unexpected}"
        )

    rows = []
    seen_keys = set()
    for name in expected:
        item = by_symbol[name]
        key = (item.key or "").strip()
        expected_segment = "NSE_FO" if item.is_index else "NSE_EQ"
        if not key or "|" not in key:
            raise ValueError(f"{name}: missing or malformed instrument_key")
        segment, _identifier = key.split("|", 1)
        if segment != expected_segment:
            raise ValueError(
                f"{name}: expected {expected_segment} candle key, got {segment}"
            )
        if key in seen_keys:
            raise ValueError(f"duplicate instrument_key: {key}")
        seen_keys.add(key)
        rows.append({
            "symbol": name,
            "instrument_key": key,
            "price_source": "index_future" if item.is_index else "cash_equity",
            "segment": segment,
            "instrument_type": "FUT" if item.is_index else "EQ",
            "expiry": item.expiry if item.is_index else "",
            "is_index": "true" if item.is_index else "false",
        })
    return rows


def write_manifest(rows, destination: Path):
    """Atomically write a downloader-compatible CSV."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with temp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp, destination)
    finally:
        if temp.exists():
            temp.unlink()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("research_instruments.csv"),
        help="output CSV path (default: research_instruments.csv)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        universe = build_universe()
        rows = manifest_rows(universe)
        write_manifest(rows, args.output)
    except Exception as exc:
        print(f"Instrument manifest not written: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {len(rows)} validated instruments to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
