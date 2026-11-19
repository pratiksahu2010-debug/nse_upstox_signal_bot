"""Telegram long-polling command handler with feedback and offline training controls."""
import csv
import io
import json
import logging
import threading
from datetime import datetime, timedelta

import requests

from .config import IST, TELEGRAM_CHAT_ID, TELEGRAM_TOKEN
from .telegram import send

log = logging.getLogger("telegram.polling")


def _memory_state(store):
    if not hasattr(store, "_bot_state"):
        store._bot_state = {}
    return store._bot_state


def _ensure_state_table(store):
    if getattr(store, "kind", "") == "postgres":
        store._run("""CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY, value JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")


def get_persistent_state(store, key, default=None):
    _ensure_state_table(store)
    if getattr(store, "kind", "") == "postgres":
        row = store._run("SELECT value FROM bot_state WHERE key=%s", (key,), "one")
        if not row:
            return default
        value = row[0]
        return json.loads(value) if isinstance(value, str) else value
    return _memory_state(store).get(key, default)



def save_training_proposal(store, proposal):
    """Persist proposal history and maintain a latest-proposal pointer."""
    history = get_persistent_state(store, "training_proposals", [])
    history = [item for item in history if str(item.get("id")) != str(proposal.get("id"))]
    history.append(proposal)
    set_persistent_state(store, "training_proposals", history[-100:])
    set_persistent_state(store, "training_proposal", proposal)


def find_training_proposal(store, proposal_id):
    history = get_persistent_state(store, "training_proposals", [])
    for proposal in history:
        if str(proposal.get("id")) == str(proposal_id):
            return proposal
    latest = get_persistent_state(store, "training_proposal", None)
    if latest and str(latest.get("id")) == str(proposal_id):
        return latest
    return None

def set_persistent_state(store, key, value):
    _ensure_state_table(store)
    if getattr(store, "kind", "") == "postgres":
        store._run("""INSERT INTO bot_state (key,value,updated_at) VALUES (%s,%s::jsonb,now())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()""",
            (key, json.dumps(value)))
    else:
        _memory_state(store)[key] = value


class TelegramPoller:
    """Single-process Telegram getUpdates consumer with persistent update offset."""

    def __init__(self, engine, store, token=None, chat_id=None):
        self.engine, self.store = engine, store
        self.token = token if token is not None else TELEGRAM_TOKEN
        self.chat_id = str(chat_id if chat_id is not None else TELEGRAM_CHAT_ID)
        self.stop_event = threading.Event()
        self.thread = None
        self.offset = int(get_persistent_state(store, "telegram_update_offset", 0) or 0)
        self.running = False

    def start(self):
        if not self.token or not self.chat_id or self.thread is not None:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, name="telegram-polling", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        self.running = False

    def _api(self, method):
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _loop(self):
        import time
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                response = requests.get(self._api("getUpdates"), params={
                    "offset": self.offset, "timeout": 25,
                    "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=35)
                if response.status_code in (401, 403):
                    log.error("Telegram polling authentication rejected (%s)", response.status_code)
                    self.stop_event.wait(60)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    raise RuntimeError("Telegram getUpdates returned ok=false")
                for update in payload.get("result", []):
                    update_id = int(update.get("update_id", 0))
                    self._handle_update(update)
                    self.offset = max(self.offset, update_id + 1)
                    set_persistent_state(self.store, "telegram_update_offset", self.offset)
                delay = 1.0
            except Exception:
                log.exception("Telegram polling failed; retrying with bounded backoff")
                self.stop_event.wait(delay)
                delay = min(30.0, delay * 2)
        self.running = False

    def _answer_callback(self, callback_id, text):
        if not callback_id or not self.token:
            return
        try:
            requests.post(self._api("answerCallbackQuery"),
                json={"callback_query_id": callback_id, "text": text}, timeout=10)
        except requests.RequestException:
            log.exception("Telegram callback acknowledgement failed")

    def _handle_update(self, update):
        callback = update.get("callback_query")
        if callback:
            chat = (callback.get("message") or {}).get("chat") or {}
            if str(chat.get("id", "")) != self.chat_id:
                return
            try:
                prefix, raw_id, label = str(callback.get("data") or "").split(":", 2)
                if prefix != "fb" or label not in {"GOOD", "BAD", "LATE", "FAKE", "SKIP"}:
                    raise ValueError("invalid feedback")
                ok = self.store.set_feedback(int(raw_id), label)
                self._answer_callback(callback.get("id"), "Feedback saved" if ok else "Signal not found")
            except (ValueError, TypeError):
                self._answer_callback(callback.get("id"), "Invalid feedback")
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")) != self.chat_id:
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()

        if command in ("/start", "/help"):
            self._reply(
                "<b>Normal NSE F&amp;O Signal Bot</b>\n"
                "/status · /signals · /stats\n"
                "/relabel <id> <GOOD|BAD|LATE|FAKE|SKIP>\n"
                "/train · /proposals · /approve <id> · /rollback <id>\n"
                "/export · /shadow on|off · /pause · /resume\n"
                "Signal-only: no order placement or execution.")
        elif command == "/status":
            status = self.engine.status()
            upstox = self.engine.client.diagnostics() if hasattr(self.engine.client, "diagnostics") else {}
            feed = getattr(self.engine, "market_stream", None)
            feed_ok = bool(feed and feed.diagnostics().get("connected"))
            self._reply(
                f"<b>Bot:</b> {'PAUSED' if getattr(self.engine, 'paused', False) else 'RUNNING'}\n"
                f"<b>Symbols:</b> {status.get('symbols', status.get('universe_size', 'n/a'))}\n"
                f"<b>Upstox auth failed:</b> {upstox.get('auth_failed', 'n/a')}\n"
                f"<b>Market feed:</b> {'connected' if feed_ok else 'not connected'}")
        elif command == "/signals":
            rows = self._recent()
            lines = ["<b>Recent signals</b>"] + [
                f"{r.get('symbol')} · {r.get('side')} · entry {r.get('entry')} · SL {r.get('sl')} · {r.get('status')}"
                for r in rows[:8]]
            self._reply("\n".join(lines) if rows else "No signal records are available.")
        elif command == "/stats":
            rows = self.store.stats(datetime.now(IST).date() - timedelta(days=30))
            if not rows:
                self._reply("No closed signal outcomes yet.")
            else:
                lines = ["<b>Last 30 days</b>"]
                for row in rows:
                    closed, wins = row.get("closed", 0), row.get("wins", 0)
                    wr = 100.0 * wins / closed if closed else 0.0
                    avg_r = row.get("sum_r", 0.0) / closed if closed else 0.0
                    mfe, mae = row.get("avg_mfe_r"), row.get("avg_mae_r")
                    excursion = f" · MFE {mfe:.2f}R / MAE {mae:.2f}R" if mfe is not None and mae is not None else ""
                    lines.append(f"{row.get('module')}: {closed} closed · {wr:.1f}% wins · {avg_r:.2f} avg R{excursion}")
                self._reply("\n".join(lines))
        elif command == "/relabel":
            if len(parts) != 3 or parts[2].upper() not in {"GOOD", "BAD", "LATE", "FAKE", "SKIP"}:
                self._reply("Usage: /relabel <signal_id> <GOOD|BAD|LATE|FAKE|SKIP>")
            else:
                try:
                    ok = self.store.set_feedback(int(parts[1]), parts[2].upper())
                except ValueError:
                    ok = False
                self._reply("Human label saved; it takes precedence over the automatic label." if ok else "Signal ID not found.")
        elif command == "/train":
            from .training import build_proposal
            rows = self.store.training_rows(datetime.now(IST).date() - timedelta(days=180))
            report = build_proposal(rows)
            proposal = report.get("proposal")
            if proposal:
                proposal["report"] = {k: report.get(k) for k in ("samples", "distinct_days", "folds", "overlap_purged", "boundary_purged", "train", "validation", "rolling_fold_scores", "fold_reports", "held_out_test", "baseline_test")}
                save_training_proposal(self.store, proposal)
                self._reply(f"<b>Proposal {proposal['id']} created.</b>\nUse /proposals to inspect. Nothing was applied.")
            else:
                self._reply(f"Trainer: {report.get('status')} · samples {report.get('samples', 0)} · overlap purged {report.get('overlap_purged', 0)} · required {report.get('required', 'n/a')}. No proposal.")
        elif command == "/proposals":
            history = get_persistent_state(self.store, "training_proposals", [])
            proposal = get_persistent_state(self.store, "training_proposal", None)
            if not history and not proposal:
                self._reply("No proposal. Run /train after enough completed outcomes.")
            else:
                lines = ["<b>Training proposals (latest 10)</b>"]
                for item in history[-10:]:
                    lines.append(f"{item.get('id')} · {item.get('state', 'PENDING')} · {item.get('diff', {})}")
                if proposal:
                    lines.append("Latest validation and held-out report:")
                    report = proposal.get("report") or {}
                    for section in ("train", "validation", "held_out_test", "baseline_test"):
                        if report.get(section):
                            lines.append(f"{section}: {report[section]}")
                lines.append("Research-only. No proposal changes the live strategy.")
                self._reply("\n".join(lines))
        elif command == "/shadow":
            if len(parts) != 2 or parts[1].lower() not in {"on", "off"}:
                self._reply("Usage: /shadow on|off")
            else:
                enabled = parts[1].lower() == "on"
                set_persistent_state(self.store, "training_shadow_mode", enabled)
                self._reply("Shadow evaluation enabled; proposed filters are logged only and cannot block alerts." if enabled else "Shadow evaluation disabled.")
        elif command in ("/approve", "/rollback"):
            proposal = find_training_proposal(self.store, parts[1]) if len(parts) == 2 else None
            if not proposal:
                self._reply(f"Usage: {command} <known_proposal_id> · inspect /proposals first.")
            else:
                proposal["state"] = "APPROVED_STAGED" if command == "/approve" else "ROLLED_BACK"
                save_training_proposal(self.store, proposal)
                self._reply("Approved proposal staged for manual review; live strategy unchanged." if command == "/approve" else "Proposal rolled back; live strategy unchanged.")
        elif command == "/export":
            rows = self.store.training_rows(datetime.now(IST).date() - timedelta(days=30))
            output = io.StringIO()
            fields = ["id", "bar_ts", "side", "status", "first_hit", "gross_r", "net_r",
                      "bars_to_tp1", "bars_to_tp2", "auto_label", "user_label", "snapshot"]
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                export_row = dict(row)
                export_row["snapshot"] = json.dumps(row.get("snapshot") or {}, separators=(",", ":"))
                writer.writerow(export_row)
            try:
                response = requests.post(
                    self._api("sendDocument"),
                    data={"chat_id": self.chat_id, "caption": "Last 30 days of signal outcomes"},
                    files={"document": ("signal_outcomes.csv", output.getvalue().encode("utf-8"), "text/csv")},
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    raise RuntimeError("Telegram sendDocument returned ok=false")
            except Exception:
                log.exception("Telegram CSV export failed")
                self._reply("CSV export failed. Check Telegram connectivity and bot permissions.")
        elif command == "/pause":
            self.engine.paused = True
            set_persistent_state(self.store, "paused", True)
            self._reply("⏸ Scanning paused. Existing signal tracking remains active.")
        elif command == "/resume":
            self.engine.paused = False
            set_persistent_state(self.store, "paused", False)
            self._reply("▶️ Scanning resumed.")
        else:
            self._reply("Unknown command. Send /help.")

    def _recent(self):
        if getattr(self.store, "kind", "") == "postgres":
            rows = self.store._run("SELECT symbol, side, entry, sl, status FROM signals ORDER BY id DESC LIMIT 8", fetch="all")
            return [{"symbol": r[0], "side": r[1], "entry": r[2], "sl": r[3], "status": r[4]} for r in rows]
        rows = sorted(getattr(self.store, "rows", []), key=lambda row: row.get("id", 0), reverse=True)
        return rows[:8]

    def _reply(self, text):
        try:
            send(text)
        except Exception:
            log.exception("Telegram command reply failed")
