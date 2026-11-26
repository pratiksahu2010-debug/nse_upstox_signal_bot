"""One-session shadow lab for comparing completed-candle entry timing.

This module is research-only: it never gates, delays, edits, or emits live signals.
Each candidate uses only bars that completed after its hypothetical entry.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta
from statistics import mean, median

from .config import ENTRY_TRAINING_ENABLED, TRAINING_COST_R, IST

log = logging.getLogger("entry_training")

_STATE_KEY = "entry_timing_training_session"
_ENABLED_KEY = "entry_timing_training_enabled"
_MAX_SIGNALS = 150
_DELAY_BARS = (0, 1, 2)
_HORIZON_BARS = 6
_MIN_REPORT_SAMPLES = 5
_MIN_IMPROVEMENT_R = 0.10
_LOCK = threading.RLock()


def _state(store):
    from .telegram_polling import get_persistent_state
    return get_persistent_state(store, _STATE_KEY, None)


def _save(store, state):
    from .telegram_polling import set_persistent_state
    set_persistent_state(store, _STATE_KEY, state)


def enabled(store):
    from .telegram_polling import get_persistent_state
    return bool(get_persistent_state(store, _ENABLED_KEY, ENTRY_TRAINING_ENABLED))


def set_enabled(store, value):
    from .telegram_polling import set_persistent_state
    set_persistent_state(store, _ENABLED_KEY, bool(value))
    return bool(value)


def _ensure_session(store, day: date):
    current = _state(store)
    day_key = day.isoformat()
    if current and current.get("date") == day_key:
        return current
    if current:
        current["status"] = "CLOSED"
        current["closed_at"] = datetime.now().astimezone().isoformat()
        _save(store, current)
        from .telegram_polling import set_persistent_state
        set_persistent_state(store, "entry_timing_training_previous", current)
    current = {
        "date": day_key,
        "status": "COLLECTING",
        "started_at": datetime.now().astimezone().isoformat(),
        "signals": {},
        "config": {
            "delay_bars": list(_DELAY_BARS),
            "horizon_bars": _HORIZON_BARS,
            "target_r": 1.5,
            "cost_r": float(TRAINING_COST_R),
        },
    }
    _save(store, current)
    return current


def _signal_record(signal_id: int, signal):
    entry = float(signal.entry)
    stop = float(signal.sl)
    target = float(signal.tp1)
    risk = abs(entry - stop)
    side = 1 if str(signal.side).upper() in ("LONG", "CALL", "CE") else -1
    if risk <= 0 or not all(map(lambda x: abs(x) < float("inf"), (entry, stop, target))):
        return None
    target_r = abs(target - entry) / risk
    return {
        "signal_id": int(signal_id),
        "symbol": str(signal.symbol),
        "side": "LONG" if side > 0 else "SHORT",
        "direction": side,
        "signal_bar_ts": signal.bar_ts.isoformat(),
        "original_entry": entry,
        "risk_points": risk,
        "target_r": target_r if target_r > 0 else 1.5,
        "score": int(getattr(signal, "score", 0) or 0),
        "trigger": str(getattr(signal, "trigger", "") or ""),
        "bars": [],
        "results": {},
    }


def record_signals(store, signals, day: date):
    """Batch-register persisted signals after live notifications have been dispatched."""
    if not signals or not enabled(store):
        return 0
    try:
        with _LOCK:
            session = _ensure_session(store, day)
            records = session.setdefault("signals", {})
            added = 0
            for signal_id, signal in signals:
                key = str(signal_id)
                if key in records or len(records) >= _MAX_SIGNALS:
                    continue
                item = _signal_record(signal_id, signal)
                if item is None:
                    continue
                records[key] = item
                added += 1
            if added:
                _save(store, session)
            return added
    except Exception:
        # Training is strictly best-effort and must never affect signal dispatch.
        log.exception("could not persist one-session entry timing records")
        return 0


def record_signal(store, signal_id: int, signal, day: date):
    """Compatibility helper for tests and one-off signal registration."""
    return bool(record_signals(store, [(signal_id, signal)], day))


def _evaluate_candidate(record, delay: int, cost_r: float):
    bars = record["bars"]
    if len(bars) < delay + _HORIZON_BARS:
        return None
    # Reject gaps rather than treating a missing candle as a five-minute bar.
    expected = datetime.fromisoformat(record["signal_bar_ts"]) + timedelta(minutes=5)
    for item in bars[:delay + _HORIZON_BARS]:
        stamp = datetime.fromisoformat(item["ts"])
        if stamp != expected:
            return None
        expected += timedelta(minutes=5)
    direction = int(record["direction"])
    risk = float(record["risk_points"])
    if delay == 0:
        entry = float(record["original_entry"])
    else:
        entry = float(bars[delay - 1]["close"])
    stop = entry - direction * risk
    target = entry + direction * risk * float(record["target_r"])
    window = bars[delay:delay + _HORIZON_BARS]
    mfe = 0.0
    mae = 0.0
    first_hit = "TIME"
    gross_r = (float(window[-1]["close"]) - entry) * direction / risk
    for bar in window:
        high, low = float(bar["high"]), float(bar["low"])
        favourable = (high - entry) * direction / risk if direction > 0 else (entry - low) / risk
        adverse = (entry - low) / risk if direction > 0 else (high - entry) / risk
        mfe = max(mfe, favourable)
        mae = max(mae, adverse)
        stop_hit = low <= stop if direction > 0 else high >= stop
        target_hit = high >= target if direction > 0 else low <= target
        # Conservative same-candle ambiguity: stop is assumed first.
        if stop_hit:
            first_hit, gross_r = "SL", -1.0
            break
        if target_hit:
            first_hit, gross_r = "TP1", float(record["target_r"])
            break
    return {
        "delay_bars": delay,
        "entry": round(entry, 6),
        "first_hit": first_hit,
        "gross_r": round(gross_r, 4),
        "net_r": round(gross_r - float(cost_r), 4),
        "mfe_r": round(mfe, 4),
        "mae_r": round(mae, 4),
        "bars_evaluated": len(window),
    }


def observe_completed_bars(store, day: date, frames: dict):
    """Append post-signal completed 5m OHLC bars and finalize mature candidates."""
    if not enabled(store):
        return 0
    with _LOCK:
        session = _ensure_session(store, day)
        records = session.get("signals") or {}
        if not records:
            return 0
        changed = 0
        dirty = False
        for record in records.values():
            if record.get("complete"):
                continue
            frame = frames.get(record.get("symbol"))
            if frame is None or getattr(frame, "empty", True):
                continue
            signal_ts = datetime.fromisoformat(record["signal_bar_ts"])
            known = {item["ts"] for item in record.get("bars", [])}
            for stamp, candle in frame.iterrows():
                try:
                    ts = stamp.isoformat()
                    if stamp <= signal_ts or ts in known or stamp.date().isoformat() != session["date"]:
                        continue
                    values = {name: float(candle[name]) for name in ("high", "low", "close")}
                    if not all(abs(value) < float("inf") for value in values.values()):
                        continue
                    record.setdefault("bars", []).append({"ts": ts, **values})
                    known.add(ts)
                    dirty = True
                except (TypeError, ValueError, KeyError):
                    continue
            record["bars"].sort(key=lambda item: item["ts"])
            for delay in _DELAY_BARS:
                key = str(delay)
                if key not in record.setdefault("results", {}):
                    result = _evaluate_candidate(record, delay, TRAINING_COST_R)
                    if result is not None:
                        record["results"][key] = result
                        changed += 1
            record["complete"] = all(str(delay) in record["results"] for delay in _DELAY_BARS)
        if dirty or changed:
            session["updated_at"] = datetime.now().astimezone().isoformat()
            _save(store, session)
        return changed


def build_daily_report(session):
    if not session:
        return {"status": "not_started", "date": None, "signals": 0, "by_delay": {}, "recommendation": None}
    records = list((session.get("signals") or {}).values())
    by_delay = {}
    for delay in _DELAY_BARS:
        rows = [record["results"][str(delay)] for record in records
                if str(delay) in (record.get("results") or {})]
        if rows:
            values = [float(row["net_r"]) for row in rows]
            by_delay[str(delay)] = {
                "samples": len(rows),
                "win_rate_pct": round(100 * sum(value > 0 for value in values) / len(values), 1),
                "mean_net_r": round(mean(values), 4),
                "median_net_r": round(median(values), 4),
                "mean_mfe_r": round(mean(float(row["mfe_r"]) for row in rows), 4),
                "mean_mae_r": round(mean(float(row["mae_r"]) for row in rows), 4),
                "tp1_first": sum(row["first_hit"] == "TP1" for row in rows),
                "sl_first": sum(row["first_hit"] == "SL" for row in rows),
                "time_exit": sum(row["first_hit"] == "TIME" for row in rows),
            }
    recommendation = None
    baseline = by_delay.get("0")
    alternatives = [
        (float(item["mean_net_r"]), int(delay), item)
        for delay, item in by_delay.items() if delay != "0"
        and item["samples"] >= _MIN_REPORT_SAMPLES
    ]
    if baseline and baseline["samples"] >= _MIN_REPORT_SAMPLES and alternatives:
        best_mean, best_delay, best = max(alternatives)
        if best_mean >= float(baseline["mean_net_r"]) + _MIN_IMPROVEMENT_R:
            recommendation = {
                "status": "candidate_timing_found_needs_multi_day_validation",
                "delay_bars": best_delay,
                "delay_minutes": best_delay * 5,
                "improvement_r": round(best_mean - float(baseline["mean_net_r"]), 4),
                "samples": best["samples"],
            }
        else:
            recommendation = {"status": "no_clear_one_day_improvement"}
    else:
        recommendation = {
            "status": "insufficient_one_day_samples",
            "required_per_cohort": _MIN_REPORT_SAMPLES,
        }
    return {
        "status": session.get("status", "COLLECTING"),
        "date": session.get("date"),
        "signals": len(records),
        "completed_signals": sum(bool(row.get("complete")) for row in records),
        "pending_signals": sum(not bool(row.get("complete")) for row in records),
        "by_delay": by_delay,
        "recommendation": recommendation,
        "research_only": True,
        "live_strategy_changed": False,
    }


def pending_symbols(store, day: date):
    """Symbols that still need completed bars for the current session's shadow cohorts."""
    if not enabled(store):
        return set()
    session = _state(store)
    if not session or session.get("date") != day.isoformat() or session.get("status") != "COLLECTING":
        return set()
    return {
        str(record.get("symbol"))
        for record in (session.get("signals") or {}).values()
        if record.get("symbol") and not record.get("complete")
    }


def current_report(store):
    session = _state(store)
    now = datetime.now(IST)
    if session and session.get("status") == "COLLECTING":
        session_day = date.fromisoformat(session["date"])
        if session_day < now.date() or now.time() >= time(15, 36):
            session["status"] = "CLOSED"
            session["closed_at"] = now.isoformat()
            _save(store, session)
            from .telegram_polling import set_persistent_state
            set_persistent_state(store, "entry_timing_training_previous", session)
    if session is None:
        from .telegram_polling import get_persistent_state
        session = get_persistent_state(store, "entry_timing_training_previous", None)
    return build_daily_report(session)


def format_daily_report(report):
    if report.get("status") == "not_started":
        return "One-day entry training has not started yet."
    lines = [
        f"<b>One-day entry timing lab · {report.get('date')}</b>",
        f"Signals {report.get('signals', 0)} · complete {report.get('completed_signals', 0)} · pending {report.get('pending_signals', 0)}",
    ]
    for delay, stats in sorted((report.get("by_delay") or {}).items(), key=lambda item: int(item[0])):
        lines.append(
            f"{int(delay) * 5}m delay: n={stats['samples']} · win {stats['win_rate_pct']:.1f}% "
            f"· avg net {stats['mean_net_r']:.2f}R · MFE {stats['mean_mfe_r']:.2f}R · MAE {stats['mean_mae_r']:.2f}R"
        )
    recommendation = report.get("recommendation") or {}
    lines.append(f"Result: {recommendation.get('status', 'no_result')}")
    if recommendation.get("delay_minutes") is not None:
        lines.append(f"Research candidate: wait {recommendation['delay_minutes']} minutes · improvement {recommendation['improvement_r']:.2f}R")
    lines.append("Shadow research only. No live entry, score, indicator, SL/TP or alert rule was changed.")
    return "\n".join(lines)
