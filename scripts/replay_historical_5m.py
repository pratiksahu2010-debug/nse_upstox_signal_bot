"""Causal, offline replay of the production Normal 5-minute signal lifecycle.

Input: one timestamped OHLCV CSV per underlying, as produced by
scripts.download_historical_5m. No broker, database, Telegram, or order APIs.
"""
import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from app.common import Ctx, SymbolInfo
from app.config import NORMAL
from app.engine import (
    _cooldown_state_after_bar,
    _recover_normal_state_after_gap,
    _triggered_to_cooldown,
)
from app.indicators import add_indicators, compute_features
from app import mod_normal
from app.tracker import simulate

log = logging.getLogger("normal_5m_replay")
INPUT_COLUMNS = ["open", "high", "low", "close", "volume"]


def load_ohlcv(path: Path) -> pd.DataFrame:
    """Load and validate a timestamped 5-minute OHLCV CSV."""
    frame = pd.read_csv(path, parse_dates=["timestamp"])
    required = {"timestamp", *INPUT_COLUMNS}
    if not required.issubset(frame.columns):
        raise ValueError(f"{path}: missing columns {sorted(required - set(frame.columns))}")
    index = pd.DatetimeIndex(frame.pop("timestamp"))
    if index.tz is None:
        index = index.tz_localize("Asia/Kolkata")
    else:
        index = index.tz_convert("Asia/Kolkata")
    frame.index = index
    frame = frame[INPUT_COLUMNS].apply(pd.to_numeric, errors="coerce")
    if frame.empty or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError(f"{path}: timestamps must be non-empty, ascending and unique")
    values = frame.to_numpy(dtype=float)
    if not pd.notna(values).all():
        raise ValueError(f"{path}: OHLCV contains missing or non-finite values")
    if (frame.volume < 0).any() or (frame.high < frame.low).any():
        raise ValueError(f"{path}: invalid volume or high/low range")
    if (frame.high < frame[["open", "close"]].max(axis=1)).any():
        raise ValueError(f"{path}: high below open/close")
    if (frame.low > frame[["open", "close"]].min(axis=1)).any():
        raise ValueError(f"{path}: low above open/close")
    if not (index.minute % 5 == 0).all() or not (index.second == 0).all():
        raise ValueError(f"{path}: timestamps are not aligned to 5-minute bars")
    return frame


def _initial_state(bar_ts):
    return {
        "phase": "IDLE", "side": 0, "last_bar": bar_ts.isoformat(),
        "cooldown_remaining": 0, "invalidated": False,
    }


def replay_frame(
    symbol: str,
    frame: pd.DataFrame,
    tick: float = 0.05,
    session_end=None,
) -> list[dict]:
    """Replay one underlying chronologically using only information available at each bar."""
    if frame.empty:
        return []
    ind = add_indicators(frame)
    info = SymbolInfo(symbol, f"RESEARCH|{symbol}", "normal", tick=tick)
    results = []
    state = None
    current_day = None

    for i in range(len(ind)):
        prefix = ind.iloc[: i + 1]
        bar_ts = prefix.index[-1]
        if current_day != bar_ts.date():
            current_day = bar_ts.date()
            state = None

        if len(prefix) < 50:
            continue
        features = compute_features(prefix)
        if features is None:
            continue

        # Live engine establishes a per-day baseline on its first eligible
        # completed candle and does not evaluate that candle as a new setup.
        if state is None:
            state = _initial_state(features.bar_ts)
            continue

        last_bar = pd.Timestamp(state["last_bar"])
        if last_bar.tzinfo is None:
            last_bar = last_bar.tz_localize("Asia/Kolkata")
        if features.bar_ts <= last_bar:
            continue

        gap = features.bar_ts - last_bar
        state, gap_reset = _recover_normal_state_after_gap(state, gap)
        phase = state.get("phase", "IDLE")
        side = int(state.get("side", 0) or 0)
        signal = None
        reject = None

        if phase == "IDLE":
            side = mod_normal.setup_side(features)
            if side:
                state.update({
                    "phase": "ARMED", "side": side,
                    "armed_bar": features.bar_ts.isoformat(),
                    "cooldown_remaining": 0, "invalidated": False,
                })
                if mod_normal.rsi_triggered(features, side):
                    signal, reject = mod_normal.evaluate(
                        info, prefix, features, Ctx(ignore_time=False)
                    )
                    if signal is not None:
                        state.update({
                            "phase": "TRIGGERED", "side": side,
                            "trigger_bar": features.bar_ts.isoformat(),
                            "cooldown_remaining": 4, "invalidated": False,
                        })
                    else:
                        state.update({"phase": "IDLE", "side": 0, "invalidated": False})
        elif phase == "ARMED":
            current_side = mod_normal.setup_side(features)
            if current_side != side:
                state.update({"phase": "IDLE", "side": 0, "invalidated": False})
            elif mod_normal.rsi_triggered(features, side):
                signal, reject = mod_normal.evaluate(
                    info, prefix, features, Ctx(ignore_time=False)
                )
                if signal is not None:
                    state.update({
                        "phase": "TRIGGERED", "side": side,
                        "trigger_bar": features.bar_ts.isoformat(),
                        "cooldown_remaining": 4, "invalidated": False,
                    })
                else:
                    state.update({"phase": "IDLE", "side": 0, "invalidated": False})
        elif phase == "TRIGGERED":
            state = _triggered_to_cooldown(state, mod_normal.setup_side(features))
        elif phase == "COOLDOWN":
            state = _cooldown_state_after_bar(state, mod_normal.setup_side(features))
        else:
            state = _initial_state(features.bar_ts)

        state["last_bar"] = features.bar_ts.isoformat()
        if signal is not None:
            outcome_row = {
                "side": signal.side,
                "bar_ts": signal.bar_ts,
                "entry": signal.entry,
                "sl": signal.sl,
                "tp1": signal.tp1,
                "tp2": signal.tp2,
            }
            status, net_r = simulate(
                outcome_row, ind.loc[ind.index.date == features.bar_ts.date()],
                session_end or NORMAL.session_end,
            )
            results.append({
                "symbol": symbol,
                "bar_ts": signal.bar_ts.isoformat(),
                "side": signal.side,
                "entry": signal.entry,
                "sl": signal.sl,
                "tp1": signal.tp1,
                "tp2": signal.tp2,
                "atr": signal.atr,
                "score": signal.score,
                "status": status,
                "net_r": net_r,
            })
        elif reject:
            log.debug("%s %s rejected: %s", symbol, features.bar_ts, reject)

    return results


def replay_directory(input_dir: Path, output: Path, tick: float = 0.05):
    """Replay every CSV in a directory and write a single signal/outcome CSV."""
    files = sorted(input_dir.glob("*.csv"))
    if not files:
        raise ValueError(f"no CSV files found in {input_dir}")
    rows = []
    for path in files:
        symbol = path.stem.upper()
        log.info("Replaying %s", symbol)
        rows.extend(replay_frame(symbol, load_ohlcv(path), tick=tick))
    output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=[
        "symbol", "bar_ts", "side", "entry", "sl", "tp1", "tp2",
        "atr", "score", "status", "net_r",
    ]).to_csv(output, index=False)
    log.info("Saved %d Normal signals to %s", len(rows), output)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("research_results/normal_5m_replay.csv"))
    parser.add_argument("--tick", type=float, default=0.05,
                        help="underlying tick size; supply per-symbol ticks for accurate tick rounding")
    args = parser.parse_args(argv)
    try:
        replay_directory(args.input_dir, args.output, args.tick)
    except (OSError, ValueError) as exc:
        log.error("Replay stopped: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
