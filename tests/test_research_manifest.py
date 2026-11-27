from types import SimpleNamespace

import pytest

from app import symbols
from scripts.build_research_manifest import FIELDS, manifest_rows, write_manifest


def _universe():
    items = []
    for name in symbols.FIXED_STOCK_SYMBOLS:
        items.append(SimpleNamespace(
            symbol=name, key=f"NSE_EQ|ISIN-{name}", is_index=False, expiry=""
        ))
    for name in symbols.FIXED_INDEX_SYMBOLS:
        items.append(SimpleNamespace(
            symbol=name, key=f"NSE_FO|TOKEN-{name}", is_index=True, expiry="31DEC26"
        ))
    return SimpleNamespace(ready=True, validation_errors=[], normal=items)


def test_manifest_is_complete_ordered_and_downloader_compatible():
    rows = manifest_rows(_universe())
    assert len(rows) == 150
    assert [row["symbol"] for row in rows] == symbols.FIXED_FNO_SYMBOLS
    assert len({row["instrument_key"] for row in rows}) == 150
    assert all(row["segment"] == "NSE_EQ" for row in rows[:144])
    assert all(row["segment"] == "NSE_FO" for row in rows[144:])
    assert rows[0]["price_source"] == "cash_equity"
    assert rows[-1]["price_source"] == "index_future"
    assert rows[-1]["expiry"] == "31DEC26"
    assert {"symbol", "instrument_key"}.issubset(FIELDS)


def test_manifest_fails_closed_when_live_universe_not_ready():
    universe = _universe()
    universe.ready = False
    universe.validation_errors = ["missing configured underlyings: ['SBIN']"]
    with pytest.raises(ValueError, match="not ready"):
        manifest_rows(universe)


def test_manifest_rejects_missing_duplicate_and_wrong_segment():
    universe = _universe()
    universe.normal.pop()
    with pytest.raises(ValueError, match="mismatch"):
        manifest_rows(universe)

    universe = _universe()
    universe.normal[-1].key = "NSE_EQ|WRONG-SEGMENT"
    with pytest.raises(ValueError, match="expected NSE_FO"):
        manifest_rows(universe)


def test_manifest_write_is_atomic_and_readable(tmp_path):
    rows = manifest_rows(_universe())
    target = tmp_path / "research_instruments.csv"
    write_manifest(rows, target)
    import csv
    with target.open(newline="", encoding="utf-8") as handle:
        saved = list(csv.DictReader(handle))
    assert len(saved) == 150
    assert saved[0]["symbol"] == symbols.FIXED_STOCK_SYMBOLS[0]
    assert saved[0]["instrument_key"].startswith("NSE_EQ|")
    assert not target.with_suffix(".csv.tmp").exists()
