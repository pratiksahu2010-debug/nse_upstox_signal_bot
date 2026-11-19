from app import symbols


def test_fixed_universe_is_exactly_150_unique_symbols():
    assert symbols.validate_fixed_universe() is True
    assert len(symbols.FIXED_FNO_SYMBOLS) == 150
    assert len(set(symbols.FIXED_FNO_SYMBOLS)) == 150
    assert len(symbols.FIXED_STOCK_SYMBOLS) == 144
    assert len(symbols.FIXED_INDEX_SYMBOLS) == 6


def test_fixed_index_list_is_explicit_and_not_discovered_at_runtime():
    assert symbols.FIXED_INDEX_SYMBOLS == [
        "NIFTY", "BANKNIFTY", "FINNIFTY", "NIFTYFPI", "MIDCPNIFTY", "NIFTYNXT50"
    ]
