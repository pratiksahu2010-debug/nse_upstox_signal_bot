from pathlib import Path

from app.config import ENGINE


def test_scanner_worker_pool_is_bounded_for_small_render_instances():
    # 24 I/O workers hide upstream latency while the shared API budget caps
    # actual request throughput independently.
    assert 8 <= ENGINE.workers <= 24


def test_candle_history_cap_covers_warm_sessions_and_current_day():
    # Six NSE sessions contain at most roughly 450 five-minute candles;
    # leave headroom for the current session without retaining 900 rows.
    assert ENGINE.max_bars_kept == 600


def test_runtime_does_not_install_unused_uvicorn_standard_extras():
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    assert "uvicorn[standard]" not in requirements
    assert "uvicorn>=" in requirements
