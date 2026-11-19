"""Telegram long-polling command handler. No webhook is registered or used."""
import json
import logging
import threading
import time
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
        store._run(
            """CREATE TABLE IF NOT EXISTS bot_state (
                   key TEXT PRIMARY KEY,
                   value JSONB NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )


def get_persistent_state(store, key, default=None):
    _ensure_state_table(store)
    if getattr(store, "kind", "") == "postgres":
        row = store._run("SELECT value FROM bot_state WHERE key=%s", (key,), "one")
        if not row:
            return default
        value = row[0]
        return json.loads(value) if isinstance(value, str) else value
    return _memory_state(store).get(key, default)


def set_persistent_state(store, key, value):
    _ensure_state_table(store)
    if getattr(store, "kind", "") == "postgres":
        store._run(
            """INSERT INTO bot_state (key, value, updated_at) VALUES (%s, %s::jsonb, now())
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
            (key, json.dumps(value)),
        )
    else:
        _memory_state(store)[key] = value


class TelegramPoller:
    """Single-process Telegram getUpdates consumer with persistent update offset."""

    def __init__(self, engine, store, token=None, chat_id=None):
        self.engine = engine
        self.store = store
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
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                response = requests.get(
                    self._api("getUpdates"),
                    params={
                        "offset": self.offset,
                        "timeout": 25,
                        "allowed_updates": json.dumps(["message"]),
                    },
                    timeout=35,
                )
                if response.status_code in (401, 403):
                    log.error("Telegram polling authentication rejected (%s); check bot token", response.status_code)
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
            except (requests.RequestException, ValueError, RuntimeError, TypeError):
                log.exception("Telegram polling failed; retrying with bounded backoff")
                self.stop_event.wait(delay)
                delay = min(30.0, delay * 2)
            except Exception:
                log.exception("unexpected Telegram polling error")
                self.stop_event.wait(delay)
                delay = min(30.0, delay * 2)
        self.running = False

    def _handle_update(self, update):
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")) != self.chat_id:
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        command = text.split()[0].split("@", 1)[0].lower()
        if command in ("/start", "/help"):
            self._reply(
                "<b>Normal NSE F&amp;O Signal Bot</b>\n"
                "/status — service and feed health\n"
                "/signals — latest signal records\n"
                "/stats — last 30 days\n"
                "/pause — pause new signal scanning\n"
                "/resume — resume scanning\n"
                "Signal-only: no order placement or execution."
            )
        elif command == "/status":
            status = self.engine.status()
            upstox = self.engine.client.diagnostics() if hasattr(self.engine.client, "diagnostics") else {}
            feed = getattr(self.engine, "market_stream", None)
            feed_ok = bool(feed and feed.diagnostics().get("connected"))
            self._reply(
                f"<b>Bot:</b> {'PAUSED' if getattr(self.engine, 'paused', False) else 'RUNNING'}\n"
                f"<b>Symbols loaded:</b> {status.get('symbols', status.get('universe_size', 'n/a'))}\n"
                f"<b>Upstox auth failed:</b> {upstox.get('auth_failed', 'n/a')}\n"
                f"<b>Market feed:</b> {'connected' if feed_ok else 'not connected'}"
            )
        elif command == "/signals":
            rows = self._recent()
            if not rows:
                self._reply("No signal records are available.")
            else:
                lines = ["<b>Recent signals</b>"]
                for row in rows[:8]:
                    lines.append(
                        f"{row.get('symbol')} · {row.get('side')} · "
                        f"entry {row.get('entry')} · SL {row.get('sl')} · {row.get('status')}"
                    )
                self._reply("\n".join(lines))
        elif command == "/stats":
            rows = self.store.stats(datetime.now(IST).date() - timedelta(days=30))
            if not rows:
                self._reply("No closed signal outcomes yet.")
            else:
                lines = ["<b>Last 30 days</b>"]
                for row in rows:
                    closed = row.get("closed", 0)
                    wins = row.get("wins", 0)
                    win_rate = 100.0 * wins / closed if closed else 0.0
                    avg_r = row.get("sum_r", 0.0) / closed if closed else 0.0
                    lines.append(
                        f"{row.get('module')}: {closed} closed · {win_rate:.1f}% wins · {avg_r:.2f} avg R"
                    )
                self._reply("\n".join(lines))
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
            rows = self.store._run(
                "SELECT symbol, side, entry, sl, status FROM signals ORDER BY id DESC LIMIT 8",
                fetch="all",
            )
            return [
                {"symbol": row[0], "side": row[1], "entry": row[2], "sl": row[3], "status": row[4]}
                for row in rows
            ]
        rows = sorted(getattr(self.store, "rows", []), key=lambda row: row.get("id", 0), reverse=True)
        return rows[:8]

    def _reply(self, text):
        try:
            send(text)
        except Exception:
            log.exception("Telegram command reply failed")
