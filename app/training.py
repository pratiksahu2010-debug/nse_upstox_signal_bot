"""Offline, bounded research over completed signal outcomes. Never mutates live strategy."""
from __future__ import annotations

from datetime import datetime
from itertools import product
from statistics import mean

MIN_TRAIN_SAMPLES = 40
MIN_TEST_SAMPLES = 12
MIN_DISTINCT_DAYS = 5
MAX_CANDIDATES = 12
OPEN_STATUSES = {"OPEN", "TP1", "EXPIRED"}
HUMAN_LABELS = {"GOOD", "BAD", "LATE", "FAKE", "SKIP"}


def effective_label(row):
    """Human feedback overrides the automatic categorical label, never measured R."""
    human = str(row.get("user_label") or "").upper()
    if human in HUMAN_LABELS:
        return human
    return row.get("auto_label")


def _label_counts(rows):
    counts = {"GOOD": 0, "BAD": 0, "LATE": 0, "FAKE": 0, "SKIP": 0,
              "WIN": 0, "LOSS": 0, "BREAKEVEN": 0, "UNLABELLED": 0}
    for row in rows:
        label = effective_label(row)
        counts[label if label in counts else "UNLABELLED"] += 1
    return counts


def _score(rows):
    # Realized net R remains the objective. Subjective labels are reported separately
    # and override auto_label for categorical reporting; they never fabricate P&L.
    values = [float(r["net_r"]) for r in rows if r.get("net_r") is not None]
    if not values:
        return None
    wins = sum(v > 0 for v in values)
    return {
        "n": len(values),
        "win_rate_pct": round(100 * wins / len(values), 1),
        "mean_net_r": round(mean(values), 4),
        "sum_net_r": round(sum(values), 4),
        "effective_labels": _label_counts(rows),
    }


def walk_forward(rows, min_train=MIN_TRAIN_SAMPLES, min_test=MIN_TEST_SAMPLES):
    """Chronological train/validation/held-out test; bounded cohort filters only."""
    eligible = [
        r for r in rows
        if r.get("bar_ts") and r.get("net_r") is not None
        and r.get("snapshot") and r.get("status") not in OPEN_STATUSES
        and effective_label(r) != "SKIP"
    ]
    eligible.sort(key=lambda r: r["bar_ts"])
    if len(eligible) < min_train + 2 * min_test:
        return {"status": "insufficient_data", "samples": len(eligible),
                "required": min_train + 2 * min_test, "proposal": None}

    n = len(eligible)
    train_end = max(min_train, int(n * .6))
    val_end = max(train_end + min_test, int(n * .8))
    train, validation, test = eligible[:train_end], eligible[train_end:val_end], eligible[val_end:]
    if len(validation) < min_test or len(test) < min_test:
        return {"status": "insufficient_data", "samples": n,
                "required": min_train + 2 * min_test, "proposal": None}

    # Search a deliberately small grid; these are post-signal cohort filters,
    # not alternate entry rules and cannot alter live alerts.
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
        tr = subset(train, candidate)
        va = subset(validation, candidate)
        te = subset(test, candidate)
        if len(tr) < min_train or len(va) < min_test or len(te) < min_test:
            continue
        train_score, validation_score, test_score = _score(tr), _score(va), _score(te)
        ranked.append((validation_score["mean_net_r"], candidate,
                       train_score, validation_score, test_score))

    if not ranked:
        return {"status": "no_candidate_met_sample_floor", "samples": n, "proposal": None}

    ranked.sort(key=lambda item: (
        item[0], item[1]["min_adx"], item[1]["min_rvol"]), reverse=True)
    _, best, train_score, validation_score, test_score = ranked[0]
    baseline = _score(test)

    # A human SKIP is excluded; other human labels override auto_label in
    # effective_labels. Empirical net R, not subjective feedback, decides profitability.
    if (not baseline or test_score["mean_net_r"] <= 0
            or test_score["mean_net_r"] <= baseline["mean_net_r"]):
        return {
            "status": "no_out_of_sample_improvement", "samples": n,
            "baseline_test": baseline, "candidate_test": test_score, "proposal": None,
        }

    return {
        "status": "proposal_ready",
        "samples": n,
        "train": train_score,
        "validation": validation_score,
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
                "modification to live entry rules. Human labels override automatic "
                "categorical labels; measured net R remains the performance objective."
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
