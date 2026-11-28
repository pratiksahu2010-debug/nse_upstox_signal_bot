"""Summarize offline Normal 5-minute replay outcomes in underlying R units.

This is a research report only: it is not an options-premium or executable P&L model.
"""
import argparse
import csv
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REQUIRED = {"symbol", "bar_ts", "option_type", "status", "net_r"}
VALID_STATUSES = {"OPEN", "TP1", "SL", "TP1_BE", "TP2", "EOD"}


def _parse_timestamp(value: str, line: int) -> datetime:
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"invalid bar_ts on CSV line {line}") from None
    if stamp.tzinfo is None:
        raise ValueError(f"bar_ts must include a timezone on CSV line {line}")
    return stamp


def load_replay(path: Path) -> list[dict]:
    rows = []
    seen = set()
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not REQUIRED.issubset(reader.fieldnames):
            missing = sorted(REQUIRED - set(reader.fieldnames or []))
            raise ValueError(f"replay CSV missing columns: {missing}")
        for line, raw in enumerate(reader, start=2):
            symbol = (raw.get("symbol") or "").strip().upper()
            option_type = (raw.get("option_type") or "").strip().upper()
            status = (raw.get("status") or "").strip().upper()
            if not symbol or option_type not in {"CALL", "PUT"}:
                raise ValueError(f"invalid symbol or option_type on CSV line {line}")
            if status not in VALID_STATUSES:
                raise ValueError(f"invalid status on CSV line {line}: {status}")
            stamp = _parse_timestamp(raw.get("bar_ts") or "", line)
            key = (symbol, stamp, option_type)
            if key in seen:
                raise ValueError(f"duplicate replay signal on CSV line {line}: {key}")
            seen.add(key)
            raw_r = (raw.get("net_r") or "").strip()
            if status == "OPEN":
                if raw_r:
                    raise ValueError(f"OPEN row must not have net_r on CSV line {line}")
                net_r = None
            else:
                try:
                    net_r = float(raw_r)
                except (TypeError, ValueError):
                    raise ValueError(f"closed row requires numeric net_r on CSV line {line}") from None
                if not math.isfinite(net_r):
                    raise ValueError(f"net_r must be finite on CSV line {line}")
            rows.append({
                "symbol": symbol, "bar_ts": stamp, "option_type": option_type,
                "status": status, "net_r": net_r,
            })
    if not rows:
        raise ValueError("replay CSV contains no signal rows")
    return rows


def _metrics(rows: list[dict], label: str) -> dict:
    closed = [row for row in rows if row["net_r"] is not None]
    values = [row["net_r"] for row in closed]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    flats = len(values) - len(wins) - len(losses)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    equity = peak = max_drawdown = 0.0
    for row in sorted(closed, key=lambda item: (item["bar_ts"], item["symbol"], item["option_type"])):
        equity += row["net_r"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "group": label,
        "signals": len(rows),
        "closed": len(closed),
        "open": len(rows) - len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "flats": flats,
        "win_rate_pct": round(100 * len(wins) / len(closed), 4) if closed else "",
        "avg_net_r": round(sum(values) / len(values), 6) if values else "",
        "total_net_r": round(sum(values), 6) if values else "",
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss else ("INF" if gross_profit else ""),
        "max_drawdown_r": round(max_drawdown, 6) if closed else "",
    }


def build_report(rows: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["symbol"]].append(row)
    report = [_metrics(rows, "ALL")]
    report.extend(_metrics(grouped[symbol], symbol) for symbol in sorted(grouped))
    return report


def write_report(rows: list[dict], output: Path) -> list[dict]:
    report = build_report(rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = list(report[0])
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="normal_5m_replay.csv")
    parser.add_argument("--output", type=Path, default=Path("research_results/normal_5m_summary.csv"))
    args = parser.parse_args(argv)
    try:
        rows = load_replay(args.input)
        report = write_report(rows, args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Wrote {len(report)} summary rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
