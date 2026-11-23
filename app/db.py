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
-- Some pre-repository production deployments used a legacy signals table
-- without the current surrogate key. Add a generated id in place; existing
-- rows are retained and receive stable ids for current application queries.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS id BIGSERIAL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_id ON signals (id);
-- Preserve compatibility with legacy NOT NULL signal_id columns on inserts.
DO $signal_id_compat$
DECLARE has_legacy_column BOOLEAN; is_identity_column BOOLEAN;
BEGIN
    SELECT EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name='signals'
          AND column_name='signal_id') INTO has_legacy_column;
    IF has_legacy_column THEN
        SELECT is_identity='YES' FROM information_schema.columns
         WHERE table_schema=current_schema() AND table_name='signals'
           AND column_name='signal_id' INTO is_identity_column;
        IF NOT COALESCE(is_identity_column,FALSE) THEN
            CREATE SEQUENCE IF NOT EXISTS signals_signal_id_compat_seq;
            PERFORM setval('signals_signal_id_compat_seq',
                GREATEST(COALESCE((SELECT MAX(signal_id)::BIGINT FROM signals),0)+1,1),FALSE);
            UPDATE signals SET signal_id=nextval('signals_signal_id_compat_seq')
             WHERE signal_id IS NULL;
            PERFORM setval('signals_signal_id_compat_seq',
                GREATEST(COALESCE((SELECT MAX(signal_id)::BIGINT FROM signals),0)+1,1),FALSE);
            ALTER TABLE signals ALTER COLUMN signal_id
                SET DEFAULT nextval('signals_signal_id_compat_seq');
        END IF;
    END IF;
END
$signal_id_compat$;
-- Evolve older signals tables in place. CREATE TABLE IF NOT EXISTS alone
-- does not add columns to a table created by an earlier bot release.
-- The production-era schema used bar_time; current code consistently uses
-- bar_ts. Rename the legacy column in place so its data and dependent indexes
--/constraints remain intact. Do this before any backfill references bar_ts.
DO $body$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'signals'
           AND column_name = 'bar_time'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'signals'
           AND column_name = 'bar_ts'
    ) THEN
        ALTER TABLE signals RENAME COLUMN bar_time TO bar_ts;
    END IF;
END
$body$;
-- Older production tables predate the module split. Preserve every row and
-- classify untagged historical records as the legacy normal signal module.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS module TEXT;
UPDATE signals SET module = 'normal' WHERE module IS NULL;
ALTER TABLE signals ALTER COLUMN module SET DEFAULT 'normal';
ALTER TABLE signals ALTER COLUMN module SET NOT NULL;
-- Some older production schemas also have a required `identity` column
-- which current inserts do not populate. Give that legacy column a safe,
-- type-aware default and backfill only NULL values; never rewrite populated
-- identities or rebuild the signals table.
DO $identity_compat$
DECLARE
    identity_data_type TEXT;
    identity_udt_name TEXT;
    identity_is_generated TEXT;
    identity_column_default TEXT;
BEGIN
    SELECT data_type, udt_name, is_identity, column_default
      INTO identity_data_type, identity_udt_name, identity_is_generated, identity_column_default
      FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'signals'
       AND column_name = 'identity';

    IF FOUND AND COALESCE(identity_is_generated, 'NO') <> 'YES'
       AND identity_column_default IS NULL THEN
        IF identity_data_type IN ('text', 'character varying', 'character') THEN
            UPDATE signals
               SET "identity" = md5(concat_ws('|', COALESCE(module, 'normal'),
                   COALESCE(symbol, ''), COALESCE(side, ''),
                   COALESCE(bar_ts::text, ''), ctid::text))
             WHERE "identity" IS NULL;
            ALTER TABLE signals ALTER COLUMN "identity"
                SET DEFAULT md5(random()::text || clock_timestamp()::text);
        ELSIF identity_udt_name = 'uuid' THEN
            UPDATE signals
               SET "identity" = md5(concat_ws('|', COALESCE(module, 'normal'),
                   COALESCE(symbol, ''), COALESCE(side, ''),
                   COALESCE(bar_ts::text, ''), ctid::text))::uuid
             WHERE "identity" IS NULL;
            ALTER TABLE signals ALTER COLUMN "identity"
                SET DEFAULT (md5(random()::text || clock_timestamp()::text)::uuid);
        ELSIF identity_data_type IN ('smallint', 'integer', 'bigint', 'numeric') THEN
            CREATE SEQUENCE IF NOT EXISTS signals_identity_compat_seq;
            PERFORM setval('signals_identity_compat_seq',
                GREATEST(COALESCE((SELECT MAX("identity")::BIGINT FROM signals), 0) + 1, 1), FALSE);
            UPDATE signals SET "identity" = nextval('signals_identity_compat_seq')
             WHERE "identity" IS NULL;
            PERFORM setval('signals_identity_compat_seq',
                GREATEST(COALESCE((SELECT MAX("identity")::BIGINT FROM signals), 0) + 1, 1), FALSE);
            ALTER TABLE signals ALTER COLUMN "identity"
                SET DEFAULT nextval('signals_identity_compat_seq');
        ELSE
            RAISE EXCEPTION 'Unsupported legacy signals.identity type: % (%)',
                identity_data_type, identity_udt_name;
        END IF;
    END IF;
END
$identity_compat$;
-- The retired pre-repository signal_claims table is not used by this bot.
-- Older production databases may still have signals.identity referencing it.
-- Keep the identity column and every existing value, but remove only that
-- obsolete FK so current signal inserts are not blocked by missing legacy claims.
DO $legacy_identity_claim_fk$
DECLARE fk_name TEXT;
BEGIN
    IF to_regclass('signal_claims') IS NOT NULL THEN
        FOR fk_name IN
            SELECT constraint_row.conname
              FROM pg_constraint AS constraint_row
             WHERE constraint_row.conrelid = 'signals'::regclass
               AND constraint_row.confrelid = to_regclass('signal_claims')
               AND constraint_row.contype = 'f'
               AND constraint_row.conkey = ARRAY[
                   (SELECT attnum FROM pg_attribute
                     WHERE attrelid = 'signals'::regclass
                       AND attname = 'identity' AND NOT attisdropped)
               ]::smallint[]
        LOOP
            EXECUTE format('ALTER TABLE signals DROP CONSTRAINT %I', fk_name);
        END LOOP;
    END IF;
END
$legacy_identity_claim_fk$;
-- Older deployments may retain a required state_cycle_id column from the
-- previous signal lifecycle schema. Backfill only NULL values and add a
-- type-aware default for future inserts; existing cycle identifiers remain
-- unchanged. This keeps the legacy constraint intact without coupling the
-- current signal engine to the retired lifecycle implementation.
DO $state_cycle_compat$
DECLARE
    cycle_data_type TEXT;
    cycle_udt_name TEXT;
    cycle_is_identity TEXT;
    cycle_is_generated TEXT;
    cycle_column_default TEXT;
BEGIN
    SELECT data_type, udt_name, is_identity, is_generated, column_default
      INTO cycle_data_type, cycle_udt_name, cycle_is_identity, cycle_is_generated, cycle_column_default
      FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'signals'
       AND column_name = 'state_cycle_id';

    IF FOUND AND COALESCE(cycle_is_identity, 'NO') <> 'YES'
       AND COALESCE(cycle_is_generated, 'NEVER') = 'NEVER'
       AND cycle_column_default IS NULL THEN
        IF cycle_data_type IN ('text', 'character varying', 'character') THEN
            UPDATE signals
               SET state_cycle_id = md5(concat_ws('|', COALESCE(module, 'normal'),
                   COALESCE(symbol, ''), COALESCE(side, ''),
                   COALESCE(bar_ts::text, ''), ctid::text))
             WHERE state_cycle_id IS NULL;
            ALTER TABLE signals ALTER COLUMN state_cycle_id
                SET DEFAULT md5(random()::text || clock_timestamp()::text);
        ELSIF cycle_udt_name = 'uuid' THEN
            UPDATE signals
               SET state_cycle_id = md5(concat_ws('|', COALESCE(module, 'normal'),
                   COALESCE(symbol, ''), COALESCE(side, ''),
                   COALESCE(bar_ts::text, ''), ctid::text))::uuid
             WHERE state_cycle_id IS NULL;
            ALTER TABLE signals ALTER COLUMN state_cycle_id
                SET DEFAULT (md5(random()::text || clock_timestamp()::text)::uuid);
        ELSIF cycle_data_type IN ('smallint', 'integer', 'bigint', 'numeric') THEN
            CREATE SEQUENCE IF NOT EXISTS signals_state_cycle_compat_seq;
            PERFORM setval('signals_state_cycle_compat_seq',
                GREATEST(COALESCE((SELECT MAX(state_cycle_id)::BIGINT FROM signals), 0) + 1, 1), FALSE);
            UPDATE signals SET state_cycle_id = nextval('signals_state_cycle_compat_seq')
             WHERE state_cycle_id IS NULL;
            PERFORM setval('signals_state_cycle_compat_seq',
                GREATEST(COALESCE((SELECT MAX(state_cycle_id)::BIGINT FROM signals), 0) + 1, 1), FALSE);
            ALTER TABLE signals ALTER COLUMN state_cycle_id
                SET DEFAULT nextval('signals_state_cycle_compat_seq');
        ELSE
            RAISE EXCEPTION 'Unsupported legacy signals.state_cycle_id type: % (%)',
                cycle_data_type, cycle_udt_name;
        END IF;
    END IF;
END
$state_cycle_compat$;
-- The deployed pre-module table also predates the lifecycle status field.
-- Add the complete set of non-key signal fields idempotently so indexes and
-- runtime queries never assume CREATE TABLE IF NOT EXISTS evolved old tables.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS sl DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS tp1 DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS tp2 DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS score INTEGER;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS trig TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS details JSONB;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'OPEN';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS r_result DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE signals ADD COLUMN IF NOT EXISTS trade_day DATE;
UPDATE signals
   SET trade_day = (bar_ts AT TIME ZONE 'Asia/Kolkata')::date
 WHERE trade_day IS NULL AND bar_ts IS NOT NULL;
ALTER TABLE signals ALTER COLUMN trade_day SET NOT NULL;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS delivery_status TEXT NOT NULL DEFAULT 'DELIVERED';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS delivery_error TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS mfe_r DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS mae_r DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS bars_held INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS time_stop_review_due BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS user_label TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS auto_label TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS net_r DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS cost_r DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS first_hit TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS bars_to_tp1 INTEGER;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS bars_to_tp2 INTEGER;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS option_tracking JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS actual_option_costs JSONB NOT NULL DEFAULT '{}'::jsonb;
-- Replace the pre-module uniqueness rule (symbol, side, bar_time).
-- Keeping it would incorrectly block a second, distinct module on the same
-- candle. Drop only that exact legacy constraint; signal rows are untouched.
DO $dedupe$
DECLARE legacy_constraint TEXT;
BEGIN
    FOR legacy_constraint IN
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'signals'::regclass
           AND contype = 'u'
           AND pg_get_constraintdef(oid) = 'UNIQUE (symbol, side, bar_ts)'
    LOOP
        EXECUTE format('ALTER TABLE signals DROP CONSTRAINT %I', legacy_constraint);
    END LOOP;
END
$dedupe$;
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_module_symbol_side_bar_ts
    ON signals (module, symbol, side, bar_ts);
CREATE INDEX IF NOT EXISTS idx_signals_day ON signals (trade_day, module);
CREATE INDEX IF NOT EXISTS idx_signals_open ON signals (status) WHERE status IN ('OPEN', 'TP1');
CREATE INDEX IF NOT EXISTS idx_signals_delivery ON signals (delivery_status) WHERE delivery_status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_signals_training ON signals (trade_day, module, status);
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
                    "side":row["side"],"score":row.get("score"),"trig":row.get("trig"),
                    "bars_held":row.get("bars_held",0),"mfe_r":row.get("mfe_r"),"mae_r":row.get("mae_r"),
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
        self.pool = psycopg2.pool.ThreadedConnectionPool(1, 8, dsn, connect_timeout=10)

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
        # Older deployments created schema_migrations with only a version
        # column. CREATE TABLE IF NOT EXISTS does not evolve an existing table,
        # so upgrade the ledger in place before any INSERT references its columns.
        # Keep legacy rows and all business data; no table is dropped or rebuilt.
        self._run("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS name TEXT")
        self._run(
            "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        )
        self._run(
            "UPDATE schema_migrations SET name = 'legacy_' || version::text WHERE name IS NULL"
        )
        self._run("ALTER TABLE schema_migrations ALTER COLUMN name SET NOT NULL")

        applied_rows = self._run("SELECT version FROM schema_migrations", fetch="all")
        applied = {int(row[0]) for row in applied_rows}

        # The compatibility reconciliation contains several ALTER/UPDATE
        # statements. Run it once per database, not on every Render restart.
        # Version 8 repairs legacy databases that recorded baseline 1 while
        # still having a partially-evolved schema.
        if 1 not in applied:
            self._run(SCHEMA)
            self._run(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                (1, "baseline"),
            )
            applied.add(1)
            schema_reconciled = True
        elif 8 not in applied:
            self._run(SCHEMA)
            schema_reconciled = True
        else:
            schema_reconciled = False

        if schema_reconciled:
            self._run(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                (8, "schema_reconciliation"),
            )
            applied.add(8)

        for version, name, sql in MIGRATIONS:
            if version in applied:
                continue
            self._run(sql)
            self._run(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                (version, name),
            )

    def load_normal_states(self, day: date) -> dict:
        rows = self._run("SELECT symbol, state FROM normal_setup_state WHERE trade_day=%s", (day,), "all")
        return {symbol: (state if isinstance(state, dict) else json.loads(state)) for symbol, state in rows}

    def save_normal_state(self, day: date, symbol: str, state: dict):
        self._run("INSERT INTO normal_setup_state (trade_day, symbol, state, updated_at) VALUES (%s,%s,%s,now()) ON CONFLICT (trade_day, symbol) DO UPDATE SET state=EXCLUDED.state, updated_at=now()", (day, symbol, json.dumps(state)))

    def add_signal(self, s: dict) -> Optional[int]:
        """Insert at most one direction for a module/symbol/candle across workers."""
        from psycopg2 import InterfaceError, OperationalError

        bar_ts = s["bar_ts"]
        if getattr(bar_ts, "tzinfo", None) is not None:
            bar_ts = bar_ts.astimezone(timezone.utc)
        lock_key = chr(31).join((s["module"], s["symbol"], bar_ts.isoformat()))
        from psycopg2 import sql as pg_sql

        # Canonical current columns plus known legacy aliases. Older production
        # tables can retain NOT NULL columns from a previous bot schema; map
        # equivalent values explicitly instead of weakening their constraints.
        values_by_column = {
            "module": s["module"], "symbol": s["symbol"], "side": s["side"],
            "bar_ts": bar_ts, "trade_day": s["trade_day"],
            "entry": s["entry"], "sl": s["sl"], "tp1": s["tp1"], "tp2": s["tp2"],
            "score": s["score"], "trig": s["trig"],
            "details": json.dumps(s.get("details", {})),
            "cost_r": float(s.get("cost_r") or 0.0),
            "option_tracking": json.dumps(s.get("option_tracking") or {}),
            "actual_option_costs": json.dumps({}),
        }
        legacy_aliases = {
            "entry_price": s.get("entry"),
            "stop_loss": s.get("sl"),
            "target1": s.get("tp1"),
            "target2": s.get("tp2"),
        }
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
                        (s["module"], s["symbol"], bar_ts),
                    )
                    if cur.fetchone():
                        conn.commit()
                        return None

                    # Inspect the live table shape so legacy aliases are supplied
                    # only when that column actually exists. Fail clearly on any
                    # other required legacy column rather than discovering them
                    # one failed signal at a time.
                    cur.execute(
                        """SELECT column_name, is_nullable, column_default,
                                  is_identity, is_generated
                             FROM information_schema.columns
                            WHERE table_schema=current_schema()
                              AND table_name='signals'"""
                    )
                    metadata = cur.fetchall()
                    present = {item[0] for item in metadata}
                    for name, value in legacy_aliases.items():
                        if name in present:
                            values_by_column[name] = value

                    missing_required = [
                        name for name, nullable, default, identity, generated in metadata
                        if nullable == "NO" and default is None
                        and identity != "YES" and generated == "NEVER"
                        and name not in values_by_column
                    ]
                    if missing_required:
                        raise RuntimeError(
                            "signals table has unsupported required legacy columns: "
                            + ", ".join(sorted(missing_required))
                        )

                    columns = list(values_by_column)
                    insert_sql = pg_sql.SQL(
                        "INSERT INTO signals ({}) VALUES ({}) "
                        "ON CONFLICT (module, symbol, side, bar_ts) DO NOTHING RETURNING id"
                    ).format(
                        pg_sql.SQL(", ").join(pg_sql.Identifier(name) for name in columns),
                        pg_sql.SQL(", ").join(pg_sql.Placeholder() for _ in columns),
                    )
                    params = tuple(values_by_column[name] for name in columns)
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
        rows=self._run("""SELECT id,module,bar_ts,symbol,side,score,trig,bars_held,mfe_r,mae_r,
            COALESCE(details->'indicator_snapshot','{}'::jsonb),status,r_result,COALESCE(net_r,r_result),
            auto_label,user_label,first_hit,bars_to_tp1,bars_to_tp2,
            COALESCE(option_tracking,'{}'::jsonb),COALESCE(actual_option_costs,'{}'::jsonb)
            FROM signals WHERE trade_day >= %s ORDER BY bar_ts,id""",(since,),"all")
        out=[]
        for row in rows:
            snap=row[10] if isinstance(row[10],dict) else json.loads(row[10] or "{}")
            out.append({"id":row[0],"module":row[1],"bar_ts":row[2],"symbol":row[3],"side":row[4],
                "score":row[5],"trig":row[6],"bars_held":row[7],"mfe_r":row[8],"mae_r":row[9],
                "snapshot":snap,"status":row[11],"gross_r":row[12],"net_r":row[13],
                "auto_label":row[14],"user_label":row[15],"first_hit":row[16],
                "bars_to_tp1":row[17],"bars_to_tp2":row[18],
                "option_tracking":row[19] if isinstance(row[19],dict) else json.loads(row[19] or "{}"),
                "actual_option_costs":row[20] if isinstance(row[20],dict) else json.loads(row[20] or "{}")})
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
        st = None
        try:
            st = PostgresStore(dsn)
            st.init()
            log.info("using Postgres")
            return st
        except Exception:
            # Close a pool opened before schema initialization failed. The caller
            # never receives this object and cannot include it in startup rollback.
            if st is not None:
                try:
                    st.close()
                except Exception:
                    log.exception("failed to close Postgres pool after initialization failure")
            log.exception("Postgres unavailable; refusing volatile fallback because DATABASE_URL is configured")
            raise
    st = MemoryStore()
    log.warning("DATABASE_URL not set / unreachable: using in-memory store (state lost on restart)")
    return st
