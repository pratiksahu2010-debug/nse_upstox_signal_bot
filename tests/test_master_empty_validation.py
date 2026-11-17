from datetime import datetime

import pytest

from app.config import IST
from app.universe import build_universe


def test_empty_nse_master_fails_closed_instead_of_building_empty_universe():
    now = datetime(2026, 11, 17, 8, 0, tzinfo=IST)

    with pytest.raises(RuntimeError, match="NSE master resolved no eligible midcap stocks or index derivatives"):
        build_universe(now, nse_rows=[])
