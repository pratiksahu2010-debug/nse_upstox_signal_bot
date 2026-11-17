"""Persistence: Postgres (Render) with an in-memory fallback for tests / DATABASE_URL unset.

Stored: every alert (dedupe via UNIQUE), cooldown/daily-cap state, and trade outcomes
(tracked by tracker.py) so /stats shows the real hit-rate of each module.
"""
import json
import logging
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
CREATE INDEX IF NOT EXISTS idx_signals_delivery ON signals (delivery_status) WHERE delivery_status = 'PENDING';
"""

COLS = "id, module, symbol, side, bar_ts, trade_day, entry, sl, tp1, tp2, score, trig, status, r_result"


def _row_to_dict(r) -> dict:
    keys = COLS.replace(" ", "").split(",")
    return dict(zip(keys, r))


class MemoryStore:
    kind = "memory"

    def __init__(self):
        self.rows: list = []
        self.lock = threading.Lock()
        self._id = 0

    def init(self):
        pass

    def add_signal(self, s: dict) -> Optional[int]:
        with self.lock:
            for r in self.rows:
                if (r["module"], r["symbol"], r["side"], r["bar_ts"]) == (s["module"], s["symbol"], s["side"], s["bar_ts"]):
                    return None
            self._id += 1
            row = dict(s, id=self._id, status="OPEN", r_result=None, closed_at=None, delivery_status="PENDING", delivery_attempts=0, delivery_error=None, delivered_at=None)
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
        for r in self.rows:
            if r["trade_day"] < day and r["status"] in ("OPEN", "TP1"):
                r["status"] = "EXPIRED"

    def last_signal_ts(self, module: str, symbol: str, side: str) -> Optional[datetime]:
        ts = [r["bar_ts"] for r in self.rows if r["module"] == module and r["symbol"] == symbol and r["side"] == side]
        return max(ts) if ts else None

    def open_signals(self, day: date, module: Optional[str] = None, symbol: Optional[str] = None) -> list:
        return [dict(r) for r in self.rows
                if r["trade_day"] == day and r["status"] in ("OPEN", "TP1")
                and (module is None or r["module"] == module) and (symbol is None or r["symbol"] == symbol)]

    def update_outcome(self, sid: int, status: str, r_result: Optional[float]):
        for r in self.rows:
            if r["id"] == sid:
                r["status"] = status
                if r_result is not None:
                    r["r_result"] = r_result

    def stats(self, since: date) -> list:
        agg: dict = {}
        for r in self.rows:
            if r["trade_day"] < since:
                continue
            a = agg.setdefault(r["module"], {"module": r["module"], "n": 0, "closed": 0, "wins": 0, "sum_r": 0.0})
            a["n"] += 1
            if r["status"] not in ("OPEN", "TP1", "EXPIRED") and r["r_result"] is not None:
                a["closed"] += 1
                a["sum_r"] += r["r_result"]
                a["wins"] += 1 if r["r_result"] > 0 else 0
        return list(agg.values())


class PostgresStore(MemoryStore):
    kind = "postgres"

    def __init__(self, dsn: str):
        import psycopg2.pool  # imported lazily so tests run without the driver
        self.dsn = dsn
        self.pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn)

    def _run(self, sql: str, params=None, fetch: str = "none"):
        conn = self.pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                out = cur.fetchall() if fetch == "all" else cur.fetchone() if fetch == "one" else None
            conn.commit()
            return out
        except Exception:
            conn.rollback()
            raise
        finally:
            self.pool.putconn(conn)

    def init(self):
        self._run(SCHEMA)
        self._run("DELETE FROM signals WHERE trade_day < CURRENT_DATE - 180")

    def add_signal(self, s: dict) -> Optional[int]:
        row = self._run(
            """INSERT INTO signals (module, symbol, side, bar_ts, trade_day, entry, sl, tp1, tp2, score, trig, details)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (module, symbol, side, bar_ts) DO NOTHING RETURNING id""",
            (s["module"], s["symbol"], s["side"], s["bar_ts"], s["trade_day"], s["entry"], s["sl"], s["tp1"],
             s["tp2"], s["score"], s["trig"], json.dumps(s.get("details", {}))), fetch="one")
        return row[0] if row else None

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
        self._run("UPDATE signals SET status='EXPIRED' WHERE status IN ('OPEN','TP1') AND trade_day < %s", (day,))

    def last_signal_ts(self, module: str, symbol: str, side: str) -> Optional[datetime]:
        row = self._run("SELECT max(bar_ts) FROM signals WHERE module=%s AND symbol=%s AND side=%s",
                        (module, symbol, side), "one")
        return row[0] if row else None

    def open_signals(self, day: date, module: Optional[str] = None, symbol: Optional[str] = None) -> list:
        sql = f"SELECT {COLS} FROM signals WHERE trade_day=%s AND status IN ('OPEN','TP1')"
        params: list = [day]
        if module:
            sql += " AND module=%s"
            params.append(module)
        if symbol:
            sql += " AND symbol=%s"
            params.append(symbol)
        return [_row_to_dict(r) for r in self._run(sql, params, "all")]

    def update_outcome(self, sid: int, status: str, r_result: Optional[float]):
        closed = status not in ("OPEN", "TP1")
        self._run("UPDATE signals SET status=%s, r_result=COALESCE(%s, r_result), "
                  "closed_at=CASE WHEN %s THEN now() ELSE closed_at END WHERE id=%s",
                  (status, r_result, closed, sid))

    def stats(self, since: date) -> list:
        rows = self._run(
            """SELECT module, count(*) AS n,
                      count(*) FILTER (WHERE r_result IS NOT NULL AND status NOT IN ('OPEN','TP1','EXPIRED')) AS closed,
                      count(*) FILTER (WHERE r_result > 0 AND status NOT IN ('OPEN','TP1','EXPIRED')) AS wins,
                      COALESCE(sum(r_result) FILTER (WHERE status NOT IN ('OPEN','TP1','EXPIRED')), 0) AS sum_r
               FROM signals WHERE trade_day >= %s GROUP BY module ORDER BY module""",
            (since,), "all")
        return [{"module": r[0], "n": r[1], "closed": r[2], "wins": r[3], "sum_r": float(r[4])} for r in rows]


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
