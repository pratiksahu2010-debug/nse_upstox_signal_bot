from datetime import datetime

from app.config import IST
from app.universe import build_universe, resolve_nse


def _index_row(name, instrument_type, expiry, key, strike=0):
    return {
        "segment": "NSE_FO",
        "underlying_symbol": name,
        "underlying_type": "INDEX",
        "instrument_type": instrument_type,
        "expiry": expiry,
        "instrument_key": key,
        "trading_symbol": f"{name}{instrument_type}",
        "lot_size": 25,
        "tick_size": 0.05,
        "strike_price": strike,
    }


def test_explicit_index_allowlist_filters_futures_and_option_records():
    today = datetime(2026, 11, 19, 9, 15, tzinfo=IST)
    expiry = datetime(2026, 11, 26, 15, 30, tzinfo=IST)
    rows = [
        _index_row("NIFTY", "FUT", expiry, "NSE_FO|NIFTY-FUT"),
        _index_row("NIFTY", "CE", expiry, "NSE_FO|NIFTY-CE", 26000),
        _index_row("NIFTY", "PE", expiry, "NSE_FO|NIFTY-PE", 26000),
        _index_row("BANKNIFTY", "FUT", expiry, "NSE_FO|BANK-FUT"),
        _index_row("BANKNIFTY", "CE", expiry, "NSE_FO|BANK-CE", 58000),
        _index_row("BANKNIFTY", "PE", expiry, "NSE_FO|BANK-PE", 58000),
    ]

    universe = resolve_nse(
        rows, today, [], [], index_allowlist=["NIFTY"], require_options=True
    )

    assert [item.symbol for item in universe.normal] == ["NIFTY"]
    assert set(universe.options) == {"NIFTY"}
    assert universe.dropped["indices"] == []


def test_fixed_universe_reports_incomplete_master_without_scanning_partial_list():
    today = datetime(2026, 11, 19, 9, 15, tzinfo=IST)

    universe = build_universe(today=today, nse_rows=[])

    assert universe.ready is False
    assert len(universe.configured_symbols) == 150
    assert len(universe.normal) == 0
    assert any("validated universe count 0 != 150" in error for error in universe.validation_errors)
