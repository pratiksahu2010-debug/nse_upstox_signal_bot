"""Regression tests for the Normal strategy's ADX(14) minimum."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("18", "22.0"),  # an old Render value must not weaken the hard floor
        ("22", "22.0"),
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


def test_normal_adx_minimum_defaults_to_22():
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
    assert result.stdout.strip() == "14 22.0"
