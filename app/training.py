"""Offline, bounded research over completed signal outcomes. Never mutates live strategy."""
from __future__ import annotations

from datetime import datetime, timedelta
from itertools import product
from statistics import mean, median
from math import ceil

MIN_TRAIN_SAMPLES = 40
MIN_TEST_SAMPLES = 12
MIN_DISTINCT_DAYS = 8
MIN_WALK_FORWARD_FOLDS = 3
MAX_CANDIDATES = 12
EMBARGO_BARS = 1
OPEN_STATUSES = {"OPEN", "TP1", "EXPIRED"}
HUMAN_LABELS = {"GOOD", "BAD", "LATE", "FAKE", "SKIP"}


def effective_label(row):
    """Human feedback overrides the automatic categorical label, never measured R."""
    human = str(row.get("user_label") or "").upper()
    if human in HUMAN_LABELS:
        return human
    return row.get("auto_label")


def _outcome_end(row):
    """Conservative end of the tracked 5-minute outcome window."""
    try:
        held_bars = max(0, int(row.get("bars_held") or 0)) + 1
    except (TypeError, ValueError):
        held_bars = 1
    return row["bar_ts"] + timedelta(minutes=5 * held_bars)


def purge_overlapping_outcomes(rows):
    """Keep earliest same-symbol signal when tracked 5m outcome windows overlap."""
    ordered = sorted(rows, key=lambda row: (row["bar_ts"], row.get("id", 0)))
    last_end_by_symbol, kept, purged = {}, [], 0
    for row in ordered:
        symbol, stamp = row.get("symbol"), row["bar_ts"]
        if not symbol:
            kept.append(row)
            continue
        if symbol in last_end_by_symbol and stamp <= last_end_by_symbol[symbol]:
            purged += 1
            continue
        last_end_by_symbol[symbol] = _outcome_end(row)
        kept.append(row)
    return kept, purged


def purge_at_boundary(rows, boundary, embargo_bars=EMBARGO_BARS):
    """Remove samples whose measured outcome reaches the next partition's embargo."""
    cutoff = boundary - timedelta(minutes=5 * max(0, int(embargo_bars)))
    kept = [row for row in rows if _outcome_end(row) < cutoff]
    return kept, len(rows) - len(kept)


def _label_counts(rows):
    counts = {"GOOD": 0, "BAD": 0, "LATE": 0, "FAKE": 0, "SKIP": 0,
              "WIN": 0, "LOSS": 0, "BREAKEVEN": 0, "UNLABELLED": 0}
    for row in rows:
        label = effective_label(row)
        counts[label if label in counts else "UNLABELLED"] += 1
    return counts


def _score(rows):
    # Realized net R is the objective; subjective labels are reported separately.
    values = [float(r["net_r"]) for r in rows if r.get("net_r") is not None]
    if not values:
        return None
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    equity = peak = max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "n": len(values),
        "win_rate_pct": round(100 * len(wins) / len(values), 1),
        "mean_net_r": round(mean(values), 4),
        "sum_net_r": round(sum(values), 4),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "avg_win_r": round(mean(wins), 4) if wins else None,
        "avg_loss_r": round(mean(losses), 4) if losses else None,
        "max_drawdown_r": round(max_drawdown, 4),
        "effective_labels": _label_counts(rows),
    }


def _split_validation_days(days, folds):
    """Split ordered development days into non-empty, contiguous validation blocks."""
    base, extra = divmod(len(days), folds)
    chunks, offset = [], 0
    for index in range(folds):
        size = base + (1 if index < extra else 0)
        chunks.append(days[offset:offset + size])
        offset += size
    return chunks


def walk_forward(rows, min_train=MIN_TRAIN_SAMPLES, min_test=MIN_TEST_SAMPLES):
    """Expanding-window walk-forward selection plus one untouched final holdout."""
    eligible = [
        r for r in rows
        if r.get("module") == "normal"
        and r.get("bar_ts") and r.get("net_r") is not None
        and r.get("snapshot") and r.get("status") not in OPEN_STATUSES
        and effective_label(r) != "SKIP"
    ]
    eligible.sort(key=lambda r: (r["bar_ts"], r.get("id", 0)))
    eligible, overlap_purged = purge_overlapping_outcomes(eligible)

    days = sorted({row["bar_ts"].date() for row in eligible})
    if len(days) < MIN_DISTINCT_DAYS:
        return {
            "status": "insufficient_distinct_days", "samples": len(eligible),
            "distinct_days": len(days), "overlap_purged": overlap_purged,
            "required_days": MIN_DISTINCT_DAYS, "proposal": None,
        }

    # The final 20% of trading days is held out from every candidate-selection step.
    holdout_start = max(1, int(len(days) * 0.8))
    development_days, holdout_days = days[:holdout_start], days[holdout_start:]
    if len(holdout_days) < 1:
        return {"status": "insufficient_holdout_days", "samples": len(eligible),
                "distinct_days": len(days), "overlap_purged": overlap_purged, "proposal": None}

    # Start with 40% of development days, then evaluate on at least three
    # contiguous, later validation blocks using an expanding training window.
    initial_train_days = max(1, int(len(development_days) * 0.4))
    validation_days = development_days[initial_train_days:]
    fold_count = min(MIN_WALK_FORWARD_FOLDS, len(validation_days))
    if fold_count < MIN_WALK_FORWARD_FOLDS:
        return {
            "status": "insufficient_walk_forward_days", "samples": len(eligible),
            "distinct_days": len(days), "development_days": len(development_days),
            "validation_days": len(validation_days), "overlap_purged": overlap_purged,
            "required_folds": MIN_WALK_FORWARD_FOLDS, "proposal": None,
        }

    validation_blocks = _split_validation_days(validation_days, fold_count)
    holdout_start_ts = min(row["bar_ts"] for row in eligible if row["bar_ts"].date() in holdout_days)
    fold_data = []
    boundary_purged = 0

    for index, block in enumerate(validation_blocks):
        block_start = block[0]
        block_rows = [row for row in eligible if row["bar_ts"].date() in set(block)]
        train_raw = [row for row in eligible if row["bar_ts"].date() < block_start]
        boundary_ts = min(row["bar_ts"] for row in block_rows)
        train_rows, purged_train = purge_at_boundary(train_raw, boundary_ts)

        # A validation label must also finish before the next validation block
        # (or the untouched holdout) begins.
        if index + 1 < len(validation_blocks):
            next_days = set(validation_blocks[index + 1])
            next_boundary = min(row["bar_ts"] for row in eligible if row["bar_ts"].date() in next_days)
        else:
            next_boundary = holdout_start_ts
        validation_rows, purged_validation = purge_at_boundary(block_rows, next_boundary)
        boundary_purged += purged_train + purged_validation

        if len(train_rows) < min_train or len(validation_rows) < min_test:
            return {
                "status": "insufficient_fold_samples", "samples": len(eligible),
                "distinct_days": len(days), "fold": index + 1,
                "train_samples": len(train_rows), "validation_samples": len(validation_rows),
                "required_train": min_train, "required_validation": min_test,
                "overlap_purged": overlap_purged, "boundary_purged": boundary_purged,
                "proposal": None,
            }
        fold_data.append({
            "train": train_rows, "validation": validation_rows,
            "train_purged": purged_train, "validation_purged": purged_validation,
            "validation_days": [day.isoformat() for day in block],
        })

    holdout = [row for row in eligible if row["bar_ts"].date() in set(holdout_days)]
    if len(holdout) < min_test:
        return {
            "status": "insufficient_holdout_samples", "samples": len(eligible),
            "distinct_days": len(days), "held_out_samples": len(holdout),
            "required": min_test, "overlap_purged": overlap_purged,
            "boundary_purged": boundary_purged, "proposal": None,
        }

    # This bounded grid only studies post-signal cohorts; it never changes
    # signal eligibility or the live strategy.
    candidates = [
        {"min_adx": adx, "min_rvol": rvol}
        for adx, rvol in product((22.0, 24.0, 26.0), (0.0, 1.0, 1.2))
    ][:MAX_CANDIDATES]

    def subset(data, candidate):
        return [
            row for row in data
            if float(row["snapshot"].get("adx", 0) or 0) >= candidate["min_adx"]
            and float(row["snapshot"].get("rvol", 0) or 0) >= candidate["min_rvol"]
        ]

    ranked = []
    for candidate in candidates:
        fold_scores = []
        valid = True
        for fold in fold_data:
            train_subset = subset(fold["train"], candidate)
            validation_subset = subset(fold["validation"], candidate)
            if len(train_subset) < min_train or len(validation_subset) < min_test:
                valid = False
                break
            fold_scores.append(_score(validation_subset)["mean_net_r"])
        if not valid:
            continue
        positive_folds = sum(score > 0 for score in fold_scores)
        if positive_folds < ceil(len(fold_scores) * 2 / 3):
            continue
        ranked.append((median(fold_scores), mean(fold_scores), candidate, fold_scores))

    if not ranked:
        return {
            "status": "no_candidate_met_rolling_sample_floor", "samples": len(eligible),
            "distinct_days": len(days), "folds": len(fold_data),
            "overlap_purged": overlap_purged, "boundary_purged": boundary_purged,
            "proposal": None,
        }

    # Selection uses only rolling development folds. The final holdout is not
    # read until the winning candidate has been fixed.
    ranked.sort(key=lambda item: (item[0], item[1], item[2]["min_adx"], item[2]["min_rvol"]), reverse=True)
    _, _, best, selected_fold_scores = ranked[0]

    first_train = subset(fold_data[0]["train"], best)
    validation_all = [
        row for fold in fold_data for row in subset(fold["validation"], best)
    ]
    held_out = subset(holdout, best)
    if len(held_out) < min_test:
        return {
            "status": "held_out_sample_below_floor", "samples": len(eligible),
            "held_out_samples": len(held_out), "required": min_test,
            "overlap_purged": overlap_purged, "boundary_purged": boundary_purged,
            "proposal": None,
        }

    train_score = _score(first_train)
    validation_score = _score(validation_all)
    test_score = _score(held_out)
    baseline = _score(holdout)

    if (not baseline or test_score["mean_net_r"] <= 0
            or test_score["mean_net_r"] <= baseline["mean_net_r"]):
        return {
            "status": "no_out_of_sample_improvement", "samples": len(eligible),
            "distinct_days": len(days), "folds": len(fold_data),
            "overlap_purged": overlap_purged, "boundary_purged": boundary_purged,
            "rolling_validation": validation_score, "baseline_test": baseline,
            "candidate_test": test_score, "proposal": None,
        }

    return {
        "status": "proposal_ready",
        "samples": len(eligible), "distinct_days": len(days),
        "folds": len(fold_data), "overlap_purged": overlap_purged,
        "boundary_purged": boundary_purged,
        "train": train_score,
        "validation": validation_score,
        "rolling_fold_scores": selected_fold_scores,
        "fold_reports": [
            {
                "validation_days": fold["validation_days"],
                "train_samples": len(fold["train"]),
                "validation_samples": len(fold["validation"]),
                "train_boundary_purged": fold["train_purged"],
                "validation_boundary_purged": fold["validation_purged"],
            }
            for fold in fold_data
        ],
        "held_out_test": test_score,
        "baseline_test": baseline,
        "proposal": {
            "scope": "research_filter_only",
            "diff": {
                "research_filters.min_adx": best["min_adx"],
                "research_filters.min_rvol": best["min_rvol"],
            },
            "note": (
                "Post-signal cohort filter research only; not a replacement for or "
                "modification to live entry rules. Candidate selection used expanding "
                "walk-forward validation only; the final chronological holdout was "
                "evaluated after selection. Human labels override automatic categorical "
                "labels; measured net R remains the performance objective."
            ),
        },
    }


def build_proposal(rows, now=None):
    now = now or datetime.now().astimezone()
    report = walk_forward(rows)
    if report.get("proposal"):
        report["proposal"]["created_at"] = now.isoformat()
        report["proposal"]["id"] = now.strftime("%Y%m%d%H%M%S%f")
        report["proposal"]["state"] = "PENDING"
    return report


def shadow_result(snapshot, proposal):
    """Evaluate a proposal against a live signal snapshot without gating or changing it."""
    if not proposal or proposal.get("state") not in {"PENDING", "APPROVED_STAGED"}:
        return None
    diff = proposal.get("diff") or {}
    try:
        adx_ok = float(snapshot.get("adx", 0) or 0) >= float(diff["research_filters.min_adx"])
        rvol_ok = float(snapshot.get("rvol", 0) or 0) >= float(diff["research_filters.min_rvol"])
    except (KeyError, TypeError, ValueError):
        return {"proposal_id": proposal.get("id"), "evaluated": False,
                "reason": "missing_filter_or_snapshot"}
    return {"proposal_id": proposal.get("id"), "evaluated": True,
            "matched": bool(adx_ok and rvol_ok)}
