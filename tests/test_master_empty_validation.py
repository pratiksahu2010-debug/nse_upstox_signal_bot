from datetime import datetime

import pytest

from app.config import IST
from app.universe import build_universe, load_mcx


def test_empty_nse_master_fails_closed_instead_of_building_empty_universe():
    now = datetime(2026, 11, 17, 8, 0, tzinfo=IST)

    with pytest.raises(RuntimeError, match="NSE instrument master resolved no configured symbols"):
        build_universe(now, nse_rows=[], mcx_rows=[])


def test_empty_mcx_master_is_preserved_as_retryable_failure():
    now = datetime(2026, 11, 17, 8, 0, tzinfo=IST)

    with pytest.raises(RuntimeError, match="MCX instrument master resolved no active futures"):
        load_mcx(now, rows=[])
