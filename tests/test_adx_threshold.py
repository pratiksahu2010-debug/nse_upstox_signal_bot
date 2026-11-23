"""Regression tests for the Normal strategy's ADX(14) minimum."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import mod_normal


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("18", "20.0"),  # stale Render values below the approved floor are clamped
        ("19", "20.0"),
        ("20", "20.0"),
        ("25", "25.0"),  # raising the threshold remains supported
    ],
)
def test_normal_adx_minimum_is_hard_floored(configured, expected):
    env = os.environ.copy()
    env["N_ADX_MIN"] = configured
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.config import IND, NORMAL; "
            "print(IND.adx_n, NORMAL.adx_min)",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"14 {expected}"


def test_normal_adx_minimum_defaults_to_20():
    env = os.environ.copy()
    env.pop("N_ADX_MIN", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.config import IND, NORMAL; "
            "print(IND.adx_n, NORMAL.adx_min)",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "14 20.0"


def test_normal_setup_side_accepts_adx_20_and_rejects_below(monkeypatch):
    monkeypatch.setattr(mod_normal, "C", SimpleNamespace(adx_min=20.0))
    common = {
        "adx": 20.0,
        "ema9": 2.0,
        "ema21": 1.0,
        "close": 2.0,
        "vwap": 1.0,
        "htf_close": 2.0,
        "htf_ema9": 1.8,
        "htf_ema21": 1.5,
    }
    assert mod_normal.setup_side(SimpleNamespace(**common)) == 1

    short = dict(common, ema9=1.0, ema21=2.0, close=1.0, vwap=2.0, htf_close=1.0, htf_ema9=1.2, htf_ema21=1.5)
    assert mod_normal.setup_side(SimpleNamespace(**short)) == -1

    below = dict(common, adx=19.99)
    assert mod_normal.setup_side(SimpleNamespace(**below)) == 0
