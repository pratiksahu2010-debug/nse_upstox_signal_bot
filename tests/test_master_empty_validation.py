from datetime import datetime

from app.config import IST
from app.universe import build_universe


def test_empty_nse_master_fails_closed_instead_of_building_empty_universe():
    now = datetime(2026, 11, 17, 8, 0, tzinfo=IST)

    universe = build_universe(now, nse_rows=[])

    assert universe.ready is False
    assert len(universe.configured_symbols) == 150
    assert len(universe.normal) == 0
    assert len(universe.validation_errors) >= 2
