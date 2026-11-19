"""Persistence: Postgres (Render) with an in-memory fallback for tests / DATABASE_URL unset.

Stored: every alert (dedupe via UNIQUE), cooldown/daily-cap state, and trade outcomes
(tracked by tracker.py) so /stats shows the real hit-rate of each module.
"""
import json
import logging
import math
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id          BIGSERIAL PRIMARY KEY,
    module      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    bar_ts      TIMESTAMPTZ NOT NULL,
    trade_day   DATE NOT NULL,
    entry       DOUBLE PRECISION,
    sl          DOUBLE PRECISION,
    tp1         DOUBLE PRECISION,
    tp2         DOUBLE PRECISION,
    score       INTEGER,
    trig        TEXT,
    details     JSONB,
    status      TEXT NOT NULL DEFAULT 'OPEN',
    r_result    DOUBLE PRECISION,
    mfe_r       DOUBLE PRECISION,
    mae_r       DOUBLE PRECISION,
    bars_held   INTEGER NOT NULL DEFAULT 0,
    time_stop_review_due BOOLEAN NOT NULL DEFAULT FALSE,
    closed_at   TIMESTAMPTZ,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivery_error TEXT,
    delivered_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (module, symbol, side, bar_ts)
);
CREATE INDEX IF NOT EXISTS idx_signals_day ON signals (trade_day, module);
CREATE INDEX IF NOT EXISTS idx_signals_open ON signals (status) WHERE status IN ('OPEN', 'TP1');
ALTER TABLE signals ADD COLUMN IF NOT EXISTS delivery_status TEXT NOT NULL DEFAULT 'DELIVERED';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS delivery_error TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS mfe_r DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS mae_r DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS bars_held INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_signals_delivery ON signals (delivery_status) WHERE delivery_status = 'PENDING';
CREATE TABLE IF NOT EXISTS normal_setup_state (trade_day DATE NOT NULL, symbol TEXT NOT NULL, state JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (trade_day, symbol));
"""

MIGRATIONS = (
    (1, "baseline", SCHEMA),
    (2, "telegram_bot_state", """CREATE TABLE IF NOT EXISTS bot_state (
        key TEXT PRIMARY KEY,
        value JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )"""),
    (3, "time_stop_review_flag", """ALTER TABLE signals
        ADD COLUMN IF NOT EXISTS time_stop_review_due BOOLEAN NOT NULL DEFAULT FALSE"""),
    (4, "training_feedback_labels", """ALTER TABLE signals
        ADD COLUMN IF NOT EXISTS user_label TEXT;
        ALTER TABLE signals ADD COLUMN IF NOT EXISTS auto_label TEXT;
        ALTER TABLE signals ADD COLUMN IF NOT EXISTS net_r DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN IF NOT EXISTS cost_r DOUBLE PRECISION NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_signals_training ON signals (trade_day, module, status)"""),
    (5, "first_target_hit", """ALTER TABLE signals ADD COLUMN IF NOT EXISTS first_hit TEXT"""),
    (6, "time_to_target_metrics", """ALTER TABLE signals
        ADD COLUMN IF NOT EXISTS bars_to_tp1 INTEGER;
        ALTER TABLE signals ADD COLUMN IF NOT EXISTS bars_to_tp2 INTEGER"""),
    (7, "option_premium_and_actual_costs", """ALTER TABLE signals
        ADD COLUMN IF NOT EXISTS option_tracking JSONB NOT NULL DEFAULT '{}'::jsonb;
        ALTER TABLE signals ADD COLUMN IF NOT EXISTS actual_option_costs JSONB NOT NULL DEFAULT '{}'::jsonb"""),
)

COLS = "id, module, symbol, side, bar_ts, trade_day, entry, sl, tp1, tp2, score, trig, status, r_result, details, option_tracking, actual_option_costs"


def _row_to_dict(r) -> dict:
    keys = COLS.replace(" ", "").split(",")
    return dict(zip(keys, r))


def _performance_report(rows: list) -> dict:
    """Build detailed outcome analytics from persisted, completed signal records."""
    closed = [
        row for row in rows
        if row.get("status") not in ("OPEN", "TP1", "EXPIRED") and row.get("r_result") is not None
    ]

    def outcome_stats(items):
        done = [
            row for row in items
            if row.get("status") not in ("OPEN", "TP1", "EXPIRED") and row.get("r_result") is not None
        ]
        wins = sum(float(row["r_result"]) > 0 for row in done)
        sum_r = sum(float(row["r_result"]) for row in done)
        mfe = [float(row["mfe_r"]) for row in items if row.get("mfe_r") is not None]
        mae = [float(row["mae_r"]) for row in items if row.get("mae_r") is not None]
        return {
            "signals": len(items),
            "closed": len(done),
            "wins": wins,
            "losses": sum(float(row["r_result"]) < 0 for row in done),
            "win_rate_pct": round(100 * wins / len(done), 1) if done else None,
            "sum_r": round(sum_r, 3),
            "avg_r": round(sum_r / len(done), 3) if done else None,
            "avg_mfe_r": round(sum(mfe) / len(mfe), 3) if mfe else None,
            "avg_mae_r": round(sum(mae) / len(mae), 3) if mae else None,
        }

    def grouped(key_fn):
        groups = {}
        for row in rows:
            key = key_fn(row)
            groups.setdefault(str(key if key not in (None, "") else "unknown"), []).append(row)
        return {key: outcome_stats(items) for key, items in sorted(groups.items())}

    ordered = sorted(
        closed,
        key=lambda row: row.get("closed_at") or row.get("bar_ts"),
    )
    equity = peak = max_drawdown = 0.0
    for row in ordered:
        equity += float(row["r_result"])
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    total = outcome_stats(rows)
    tp1_status = {"TP1", "TP1_BE", "TP2"}
    total.update({
        "total_signals": len(rows),
        "call_signals": sum(row.get("side") == "LONG" for row in rows),
        "put_signals": sum(row.get("side") == "SHORT" for row in rows),
        "tp1_hits": sum(row.get("status") in tp1_status for row in rows),
        "tp2_hits": sum(row.get("status") == "TP2" for row in rows),
        "stop_hits": sum(row.get("status") == "SL" for row in rows),
        "time_stop_review_due": sum(bool(row.get("time_stop_review_due")) for row in rows),
        "max_drawdown_r": round(max_drawdown, 3),
    })

    def hour_bucket(row):
        stamp = row.get("bar_ts")
        return f"{stamp.hour:02d}:00-{stamp.hour:02d}:59" if stamp is not None else "unknown"

    return {
        "summary": total,
        "by_direction": grouped(lambda row: "CALL" if row.get("side") == "LONG" else "PUT" if row.get("side") == "SHORT" else row.get("side")),
        "by_symbol": grouped(lambda row: row.get("symbol")),
        "by_setup": grouped(lambda row: row.get("trig")),
        "by_time_of_day": grouped(hour_bucket),
        "by_score": grouped(lambda row: row.get("score")),
    }


class MemoryStore:
    kind = "memory"

    def __init__(self):
        self.rows: list = []
        self.lock = threading.Lock()
        self._id = 0
        self.normal_states: dict = {}

    def init(self):
        pass

    def add_signal(self, s: dict) -> Optional[int]:
        with self.lock:
            for r in self.rows:
                if (r["module"], r["symbol"], r["bar_ts"]) == (s["module"], s["symbol"], s["bar_ts"]):
                    return None
            self._id += 1
            row = dict(s, id=self._id, status="OPEN", r_result=None, net_r=None, cost_r=float(s.get("cost_r") or 0.0), auto_label=None, user_label=None, mfe_r=0.0, mae_r=0.0, bars_held=0, bars_to_tp1=None, bars_to_tp2=None, option_tracking=dict(s.get("option_tracking") or {}), actual_option_costs={}, time_stop_review_due=False, closed_at=None, delivery_status="PENDING", delivery_attempts=0, delivery_error=None, delivered_at=None)
            self.rows.append(row)
            return self._id

    def pending_notifications(self, limit: int = 25) -> list:
        with self.lock:
            return [{"id": r["id"], "text": (r.get("details") or {}).get("telegram_text")}
                    for r in self.rows if r.get("delivery_status") == "PENDING"
                    and (r.get("details") or {}).get("telegram_text")][:limit]

    def mark_notification(self, sid: int, delivered: bool, error: Optional[str] = None):
        with self.lock:
            for r in self.rows:
                if r["id"] == sid:
                    r["delivery_attempts"] = r.get("delivery_attempts", 0) + 1
                    r["delivery_status"] = "DELIVERED" if delivered else "PENDING"
                    r["delivery_error"] = None if delivered else str(error or "delivery failed")[:300]
                    if delivered:
                        r["delivered_at"] = datetime.now(timezone.utc)
                    return

    def count_today(self, module: str, day: date) -> int:
        return sum(1 for r in self.rows if r["module"] == module and r["trade_day"] == day)

    def signaled_today(self, module: str, symbol: str, day: date) -> bool:
        return any(r["module"] == module and r["symbol"] == symbol and r["trade_day"] == day for r in self.rows)

    def symbols_today(self, module: str, day: date) -> set:
        return {r["symbol"] for r in self.rows if r["module"] == module and r["trade_day"] == day}

    def expire_old(self, day: date):
        with self.lock:
            for r in self.rows:
                if r["trade_day"] < day and r["status"] in ("OPEN", "TP1"):
                    r["status"] = "EXPIRED"
                    r["closed_at"] = r.get("closed_at") or datetime.now(timezone.utc)

    def last_signal_ts(self, module: str, symbol: str, side: str) -> Optional[datetime]:
        ts = [r["bar_ts"] for r in self.rows if r["module"] == module and r["symbol"] == symbol and r["side"] == side]
        return max(ts) if ts else None

    def open_signals(self, day: date, module: Optional[str] = None, symbol: Optional[str] = None) -> list:
        with self.lock:
            return [dict(r) for r in self.rows
                    if r["trade_day"] == day and r["status"] in ("OPEN", "TP1")
                    and (module is None or r["module"] == module) and (symbol is None or r["symbol"] == symbol)]

    def update_outcome(self, sid: int, status: str, r_result: Optional[float]):
        label = "WIN" if r_result is not None and r_result > 0 else "LOSS" if r_result is not None and r_result < 0 else "BREAKEVEN" if r_result is not None else None
        with self.lock:
            for r in self.rows:
                if r["id"] == sid:
                    r["status"] = status
                    first_hit = "SL" if status == "SL" else "TP1" if status in ("TP1", "TP1_BE", "TP2") else None
                    if first_hit and not r.get("first_hit"): r["first_hit"] = first_hit
                    if r_result is not None:
                        r["r_result"] = r_result
                        r["net_r"] = float(r_result) - float(r.get("cost_r") or 0.0)
                        r["auto_label"] = label
                    if status not in ("OPEN", "TP1"):
                        r["closed_at"] = r.get("closed_at") or datetime.now(timezone.utc)
                    return

    def set_feedback(self, sid: int, label: str) -> bool:
        if label not in {"GOOD", "BAD", "LATE", "FAKE", "SKIP"}: return False
        with self.lock:
            for row in self.rows:
                if int(row["id"]) == int(sid):
                    row["user_label"] = label
                    return True
        return False

    def training_rows(self, since: date) -> list:
        with self.lock:
            out=[]
            for row in self.rows:
                if row["trade_day"] < since: continue
                details=row.get("details") or {}
                out.append({"id":row["id"],"module":row.get("module"),"bar_ts":row["bar_ts"],"symbol":row.get("symbol"),
                    "side":row["side"],"bars_held":row.get("bars_held",0),
                    "snapshot":details.get("indicator_snapshot",{}),"status":row.get("status"),
                    "gross_r":row.get("r_result"),"net_r":row.get("net_r",row.get("r_result")),
                    "auto_label":row.get("auto_label"),"user_label":row.get("user_label"),"first_hit":row.get("first_hit"),
                    "bars_to_tp1":row.get("bars_to_tp1"),"bars_to_tp2":row.get("bars_to_tp2"),
                    "option_tracking":dict(row.get("option_tracking") or {}),
                    "actual_option_costs":dict(row.get("actual_option_costs") or {})})
            return out

    def update_excursions(self, sid: int, mfe_r: float, mae_r: float, bars_held: int,
                          time_stop_review_due: bool = False):
        with self.lock:
            for row in self.rows:
                if row["id"] == sid:
                    row["mfe_r"] = max(float(row.get("mfe_r") or 0.0), float(mfe_r))
                    row["mae_r"] = max(float(row.get("mae_r") or 0.0), float(mae_r))
                    row["bars_held"] = max(int(row.get("bars_held") or 0), int(bars_held))
                    row["time_stop_review_due"] = bool(row.get("time_stop_review_due") or time_stop_review_due)
                    return

    def update_target_times(self, sid: int, bars_to_tp1: Optional[int], bars_to_tp2: Optional[int]):
        with self.lock:
            for row in self.rows:
                if row["id"] == sid:
                    if bars_to_tp1 is not None and row.get("bars_to_tp1") is None:
                        row["bars_to_tp1"] = int(bars_to_tp1)
                    if bars_to_tp2 is not None and row.get("bars_to_tp2") is None:
                        row["bars_to_tp2"] = int(bars_to_tp2)
                    return

    def update_option_tracking(self, sid: int, tracking: dict):
        with self.lock:
            for row in self.rows:
                if row["id"] == sid:
                    row["option_tracking"] = dict(tracking or {})
                    return True
        return False

    def update_actual_option_costs(self, sid: int, quantity: int, brokerage_rupees: float,
                                   other_fees_rupees: float, slippage_rupees: float,
                                   updated_at: str) -> bool:
        if quantity <= 0 or not all(math.isfinite(float(v)) and float(v) >= 0
                                   for v in (brokerage_rupees, other_fees_rupees, slippage_rupees)):
            return False
        total = float(brokerage_rupees) + float(other_fees_rupees) + float(slippage_rupees)
        costs = {
            "quantity": int(quantity), "brokerage_rupees": float(brokerage_rupees),
            "other_fees_rupees": float(other_fees_rupees),
            "slippage_rupees": float(slippage_rupees), "total_cost_rupees": total,
            "cost_per_unit": total / int(quantity), "updated_at": updated_at,
            "source": "MANUAL_BROKER_RECONCILIATION",
        }
        with self.lock:
            for row in self.rows:
                if row["id"] == sid:
                    tracking = row.get("option_tracking") or {}
                    if not tracking.get("instrument_key"):
                        return False
                    row["actual_option_costs"] = costs
                    return True
        return False

    def load_normal_states(self, day: date) -> dict:
        with self.lock:
            return {symbol: dict(state) for (trade_day, symbol), state in self.normal_states.items() if trade_day == day}

    def save_normal_state(self, day: date, symbol: str, state: dict):
        with self.lock:
            self.normal_states[(day, symbol)] = dict(state)

    def performance_report(self, since: date) -> dict:
        with self.lock:
            rows = [dict(row) for row in self.rows if row["trade_day"] >= since]
        return _performance_report(rows)

    def stats(self, since: date) -> list:
        agg: dict = {}
        with self.lock:
            for r in self.rows:
                if r["trade_day"] < since:
                    continue
                a = agg.setdefault(r["module"], {
                    "module": r["module"], "n": 0, "closed": 0, "wins": 0, "sum_r": 0.0,
                    "_mfe_sum": 0.0, "_mae_sum": 0.0, "_excursion_count": 0,
                })
                a["n"] += 1
                if r.get("mfe_r") is not None and r.get("mae_r") is not None:
                    a["_mfe_sum"] += float(r["mfe_r"])
                    a["_mae_sum"] += float(r["mae_r"])
                    a["_excursion_count"] += 1
                if r["status"] not in ("OPEN", "TP1", "EXPIRED") and r["r_result"] is not None:
                    a["closed"] += 1
                    a["sum_r"] += r["r_result"]
                    a["wins"] += 1 if r["r_result"] > 0 else 0
        result = []
        for row in agg.values():
            count = row.pop("_excursion_count")
            mfe_sum = row.pop("_mfe_sum")
            mae_sum = row.pop("_mae_sum")
            row["avg_mfe_r"] = round(mfe_sum / count, 3) if count else None
            row["avg_mae_r"] = round(mae_sum / count, 3) if count else None
            result.append(row)
        return result


class PostgresStore(MemoryStore):
    kind = "postgres"

    def __init__(self, dsn: str):
        import psycopg2.pool
        self.dsn = dsn
        self.pool = psycopg2.pool.ThreadedConnectionPool(1, 8, dsn)

    def _run(self, sql: str, params=None, fetch: str = "none"):
        from psycopg2 import InterfaceError, OperationalError
        for attempt in range(2):
            conn = self.pool.getconn()
            discard = False
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    out = cur.fetchall() if fetch == "all" else cur.fetchone() if fetch == "one" else None
                conn.commit()
                return out
            except (OperationalError, InterfaceError):
                discard = True
                try:
                    conn.rollback()
                except Exception:
                    pass
                if attempt == 0:
                    log.warning("Postgres connection failed; discarding connection and retrying once")
                    continue
                raise
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    discard = True
                raise
            finally:
                self.pool.putconn(conn, close=discard or bool(conn.closed))

    def close(self):
        """Close pooled connections during graceful process shutdown."""
        self.pool.closeall()

    def init(self):
        self._run(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
        applied_rows = self._run("SELECT version FROM schema_migrations", fetch="all")
        applied = {int(row[0]) for row in applied_rows}
        for version, name, sql in MIGRATIONS:
            if version in applied:
                continue
            self._run(sql)
            self._run(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                (version, name),
            )
        self._run("DELETE FROM signals WHERE trade_day < CURRENT_DATE - 180")

    def load_normal_states(self, day: date) -> dict:
        rows = self._run("SELECT symbol, state FROM normal_setup_state WHERE trade_day=%s", (day,), "all")
        return {symbol: (state if isinstance(state, dict) else json.loads(state)) for symbol, state in rows}

    def save_normal_state(self, day: date, symbol: str, state: dict):
        self._run("INSERT INTO normal_setup_state (trade_day, symbol, state, updated_at) VALUES (%s,%s,%s,now()) ON CONFLICT (trade_day, symbol) DO UPDATE SET state=EXCLUDED.state, updated_at=now()", (day, symbol, json.dumps(state)))

    def add_signal(self, s: dict) -> Optional[int]:
        """Insert at most one direction for a module/symbol/candle across workers."""
        from psycopg2 import InterfaceError, OperationalError

        lock_key = f'{s["module"]}\\x1f{s["symbol"]}\\x1f{s["bar_ts"].isoformat()}'
        insert_sql = """INSERT INTO signals (module, symbol, side, bar_ts, trade_day, entry, sl, tp1, tp2, score, trig, details, cost_r, option_tracking, actual_option_costs)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
                        ON CONFLICT (module, symbol, side, bar_ts) DO NOTHING RETURNING id"""
        params = (
            s["module"], s["symbol"], s["side"], s["bar_ts"], s["trade_day"],
            s["entry"], s["sl"], s["tp1"], s["tp2"], s["score"], s["trig"],
            json.dumps(s.get("details", {})), float(s.get("cost_r") or 0.0),
            json.dumps(s.get("option_tracking") or {}), json.dumps({}),
        )
        for attempt in range(2):
            conn = self.pool.getconn()
            discard = False
            try:
                with conn.cursor() as cur:
                    # Transaction-scoped lock closes the race between the existence
                    # check and INSERT, including overlapping app instances/workers.
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (lock_key,),
                    )
                    cur.execute(
                        "SELECT id FROM signals WHERE module=%s AND symbol=%s AND bar_ts=%s LIMIT 1",
                        (s["module"], s["symbol"], s["bar_ts"]),
                    )
                    if cur.fetchone():
                        conn.commit()
                        return None
                    cur.execute(insert_sql, params)
                    row = cur.fetchone()
                conn.commit()
                return row[0] if row else None
            except (OperationalError, InterfaceError):
                discard = True
                try:
                    conn.rollback()
                except Exception:
                    pass
                if attempt == 0:
                    log.warning("Postgres connection failed during signal insert; retrying once")
                    continue
                raise
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    discard = True
                raise
            finally:
                self.pool.putconn(conn, close=discard or bool(conn.closed))
        return None

    def pending_notifications(self, limit: int = 100) -> list:
        rows = self._run("SELECT id, details->>'telegram_text' FROM signals WHERE delivery_status='PENDING' "
                         "AND COALESCE(details->>'telegram_text', '') <> '' ORDER BY id LIMIT %s", (limit,), "all")
        return [{"id": r[0], "text": r[1]} for r in rows]

    def mark_notification(self, sid: int, delivered: bool, error: Optional[str] = None):
        status = "DELIVERED" if delivered else "PENDING"
        self._run("UPDATE signals SET delivery_status=%s, delivery_attempts=delivery_attempts+1, "
                  "delivery_error=%s, delivered_at=CASE WHEN %s THEN now() ELSE delivered_at END WHERE id=%s",
                  (status, None if delivered else str(error or "delivery failed")[:300], delivered, sid))

    def count_today(self, module: str, day: date) -> int:
        return self._run("SELECT count(*) FROM signals WHERE module=%s AND trade_day=%s", (module, day), "one")[0]

    def signaled_today(self, module: str, symbol: str, day: date) -> bool:
        return self._run("SELECT 1 FROM signals WHERE module=%s AND symbol=%s AND trade_day=%s LIMIT 1",
                         (module, symbol, day), "one") is not None

    def symbols_today(self, module: str, day: date) -> set:
        rows = self._run("SELECT symbol FROM signals WHERE module=%s AND trade_day=%s", (module, day), "all")
        return {r[0] for r in rows}

    def expire_old(self, day: date):
        self._run("UPDATE signals SET status='EXPIRED', closed_at=COALESCE(closed_at, now()) "
                  "WHERE status IN ('OPEN','TP1') AND trade_day < %s", (day,))

    def last_signal_ts(self, module: str, symbol: str, side: str) -> Optional[datetime]:
        row = self._run("SELECT max(bar_ts) FROM signals WHERE module=%s AND symbol=%s AND side=%s",
                        (module, symbol, side), "one")
        return row[0] if row else None

    def open_signals(self, day: date, module: Optional[str] = None, symbol: Optional[str] = None) -> list:
        # Append all filters before ORDER BY; SQL clause order is WHERE -> ORDER BY.
        sql = f"SELECT {COLS} FROM signals WHERE trade_day=%s AND status IN ('OPEN','TP1')"
        params: list = [day]
        if module is not None:
            sql += " AND module=%s"
            params.append(module)
        if symbol is not None:
            sql += " AND symbol=%s"
            params.append(symbol)
        sql += " ORDER BY bar_ts, id"
        return [_row_to_dict(r) for r in self._run(sql, params, "all")]

    def update_outcome(self, sid: int, status: str, r_result: Optional[float]):
        closed = status not in ("OPEN", "TP1")
        label = "WIN" if r_result is not None and r_result > 0 else "LOSS" if r_result is not None and r_result < 0 else "BREAKEVEN" if r_result is not None else None
        self._run("UPDATE signals SET status=%s, r_result=COALESCE(%s, r_result), "
                  "net_r=CASE WHEN %s IS NULL THEN net_r ELSE %s-COALESCE(cost_r,0) END, "
                  "auto_label=COALESCE(%s,auto_label), first_hit=COALESCE(first_hit,%s), "
                  "closed_at=CASE WHEN %s THEN now() ELSE closed_at END WHERE id=%s",
                  (status, r_result, r_result, r_result, label, ("SL" if status == "SL" else "TP1" if status in ("TP1", "TP1_BE", "TP2") else None), closed, sid))

    def set_feedback(self, sid: int, label: str) -> bool:
        if label not in {"GOOD","BAD","LATE","FAKE","SKIP"}: return False
        row=self._run("UPDATE signals SET user_label=%s WHERE id=%s RETURNING id",(label,sid),"one")
        return bool(row)

    def training_rows(self, since: date) -> list:
        rows=self._run("""SELECT id,module,bar_ts,symbol,side,bars_held,COALESCE(details->'indicator_snapshot','{}'::jsonb),
            status,r_result,COALESCE(net_r,r_result),auto_label,user_label,first_hit,bars_to_tp1,bars_to_tp2,
            COALESCE(option_tracking,'{}'::jsonb),COALESCE(actual_option_costs,'{}'::jsonb)
            FROM signals WHERE trade_day >= %s ORDER BY bar_ts,id""",(since,),"all")
        out=[]
        for row in rows:
            snap=row[6] if isinstance(row[6],dict) else json.loads(row[6] or "{}")
            out.append({"id":row[0],"module":row[1],"bar_ts":row[2],"symbol":row[3],"side":row[4],"bars_held":row[5],
                "snapshot":snap,"status":row[7],"gross_r":row[8],"net_r":row[9],
                "auto_label":row[10],"user_label":row[11],"first_hit":row[12],
                "bars_to_tp1":row[13],"bars_to_tp2":row[14],
                "option_tracking":row[15] if isinstance(row[15],dict) else json.loads(row[15] or "{}"),
                "actual_option_costs":row[16] if isinstance(row[16],dict) else json.loads(row[16] or "{}")})
        return out

    def update_excursions(self, sid: int, mfe_r: float, mae_r: float, bars_held: int,
                          time_stop_review_due: bool = False):
        self._run(
            """UPDATE signals
               SET mfe_r=GREATEST(COALESCE(mfe_r, 0), %s),
                   mae_r=GREATEST(COALESCE(mae_r, 0), %s),
                   bars_held=GREATEST(COALESCE(bars_held, 0), %s),
                   time_stop_review_due=(time_stop_review_due OR %s)
               WHERE id=%s""",
            (float(mfe_r), float(mae_r), int(bars_held), bool(time_stop_review_due), sid),
        )

    def update_target_times(self, sid: int, bars_to_tp1: Optional[int], bars_to_tp2: Optional[int]):
        self._run(
            """UPDATE signals
               SET bars_to_tp1=COALESCE(bars_to_tp1, %s),
                   bars_to_tp2=COALESCE(bars_to_tp2, %s)
               WHERE id=%s""",
            (bars_to_tp1, bars_to_tp2, sid),
        )

    def update_option_tracking(self, sid: int, tracking: dict):
        self._run(
            """UPDATE signals SET option_tracking=COALESCE(option_tracking,'{}'::jsonb) || %s::jsonb
               WHERE id=%s""",
            (json.dumps(tracking or {}), sid),
        )
        return True

    def update_actual_option_costs(self, sid: int, quantity: int, brokerage_rupees: float,
                                   other_fees_rupees: float, slippage_rupees: float,
                                   updated_at: str) -> bool:
        if quantity <= 0 or not all(math.isfinite(float(v)) and float(v) >= 0
                                   for v in (brokerage_rupees, other_fees_rupees, slippage_rupees)):
            return False
        total = float(brokerage_rupees) + float(other_fees_rupees) + float(slippage_rupees)
        costs = {
            "quantity": int(quantity), "brokerage_rupees": float(brokerage_rupees),
            "other_fees_rupees": float(other_fees_rupees),
            "slippage_rupees": float(slippage_rupees), "total_cost_rupees": total,
            "cost_per_unit": total / int(quantity), "updated_at": updated_at,
            "source": "MANUAL_BROKER_RECONCILIATION",
        }
        row = self._run(
            """UPDATE signals SET actual_option_costs=%s::jsonb
               WHERE id=%s AND COALESCE(option_tracking,'{}'::jsonb) ? 'instrument_key'
               RETURNING id""",
            (json.dumps(costs), sid), "one",
        )
        return bool(row)

    def performance_report(self, since: date) -> dict:
        rows = self._run(
            """SELECT module, symbol, side, bar_ts, trade_day, score, trig, status,
                      r_result, mfe_r, mae_r, bars_held, time_stop_review_due, closed_at
               FROM signals WHERE trade_day >= %s ORDER BY bar_ts, id""",
            (since,), "all",
        )
        keys = (
            "module", "symbol", "side", "bar_ts", "trade_day", "score", "trig",
            "status", "r_result", "mfe_r", "mae_r", "bars_held",
            "time_stop_review_due", "closed_at",
        )
        return _performance_report([dict(zip(keys, row)) for row in rows])

    def stats(self, since: date) -> list:
        rows = self._run(
            """SELECT module, count(*) AS n,
                      count(*) FILTER (WHERE r_result IS NOT NULL AND status NOT IN ('OPEN','TP1','EXPIRED')) AS closed,
                      count(*) FILTER (WHERE r_result > 0 AND status NOT IN ('OPEN','TP1','EXPIRED')) AS wins,
                      COALESCE(sum(r_result) FILTER (WHERE status NOT IN ('OPEN','TP1','EXPIRED')), 0) AS sum_r,
                      AVG(mfe_r) AS avg_mfe_r,
                      AVG(mae_r) AS avg_mae_r
               FROM signals WHERE trade_day >= %s GROUP BY module ORDER BY module""",
            (since,), "all")
        return [
            {
                "module": r[0], "n": r[1], "closed": r[2], "wins": r[3],
                "sum_r": float(r[4]),
                "avg_mfe_r": round(float(r[5]), 3) if r[5] is not None else None,
                "avg_mae_r": round(float(r[6]), 3) if r[6] is not None else None,
            }
            for r in rows
        ]


def make_store(dsn: str):
    if dsn:
        try:
            st = PostgresStore(dsn)
            st.init()
            log.info("using Postgres")
            return st
        except Exception:
            log.exception("Postgres unavailable; refusing volatile fallback because DATABASE_URL is configured")
            raise
    st = MemoryStore()
    log.warning("DATABASE_URL not set / unreachable: using in-memory store (state lost on restart)")
    return st
