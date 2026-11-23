from types import SimpleNamespace

import pandas as pd

from app.engine import _momentum_without_persisted_normal


def test_momentum_same_bar_is_suppressed_only_after_normal_is_persisted():
    bar = pd.Timestamp("2026-11-23 10:00:00+05:30")
    candidate = SimpleNamespace(symbol="NIFTY", bar_ts=bar)
    persisted_normal = {("NIFTY", bar.isoformat())}

    assert _momentum_without_persisted_normal([candidate], persisted_normal) == []
    # If Normal failed option/liquidity validation or persistence, Momentum remains eligible.
    assert _momentum_without_persisted_normal([candidate], set()) == [candidate]


def test_momentum_candidate_on_another_bar_is_not_suppressed():
    bar = pd.Timestamp("2026-11-23 10:05:00+05:30")
    candidate = SimpleNamespace(symbol="NIFTY", bar_ts=bar)
    persisted_normal = {("NIFTY", "2026-11-23T10:00:00+05:30")}

    assert _momentum_without_persisted_normal([candidate], persisted_normal) == [candidate]
