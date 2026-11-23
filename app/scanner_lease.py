"""PostgreSQL-backed leader lease for the in-process market scanner.

A Render rolling deploy can briefly run old and new instances together. The
signal table's unique key prevents duplicate alerts, but it does not prevent
both processes from consuming the Upstox request budget. This lease allows
only one process to execute a scan boundary at a time.
"""
import logging
import threading
import uuid

log = logging.getLogger("scanner.lease")


class ScannerLease:
    LEASE_KEY = "nse_fno_scanner"
    LEASE_SECONDS = 120
    RENEW_SECONDS = 20

    def __init__(self, store, lease_seconds=None, renew_seconds=None):
        self.store = store
        self.lease_seconds = int(lease_seconds or self.LEASE_SECONDS)
        self.renew_seconds = int(renew_seconds or self.RENEW_SECONDS)
        self.owner = uuid.uuid4().hex
        self.acquired = False
        self._table_ready = False
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = None

    @property
    def lost(self):
        return self._lost.is_set()

    def _ensure_table(self):
        if self._table_ready:
            return
        self.store._run(
            """CREATE TABLE IF NOT EXISTS scanner_leases (
                lease_key TEXT PRIMARY KEY,
                owner_token TEXT NOT NULL,
                lease_until TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )"""
        )
        self._table_ready = True

    def acquire(self):
        """Acquire atomically; database errors fail closed."""
        if getattr(self.store, "kind", "") != "postgres":
            self.acquired = True
            return True
        try:
            self._ensure_table()
            row = self.store._run(
                """INSERT INTO scanner_leases
                       (lease_key, owner_token, lease_until, updated_at)
                   VALUES (%s, %s, now() + (%s * interval '1 second'), now())
                   ON CONFLICT (lease_key) DO UPDATE
                       SET owner_token=EXCLUDED.owner_token,
                           lease_until=EXCLUDED.lease_until,
                           updated_at=now()
                     WHERE scanner_leases.lease_until <= now()
                        OR scanner_leases.owner_token = EXCLUDED.owner_token
                   RETURNING owner_token""",
                (self.LEASE_KEY, self.owner, self.lease_seconds),
                "one",
            )
            self.acquired = bool(row and row[0] == self.owner)
            return self.acquired
        except Exception:
            self.acquired = False
            log.exception("scanner leader lease acquisition failed; scan skipped")
            return False

    def start_heartbeat(self):
        if not self.acquired or getattr(self.store, "kind", "") != "postgres":
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat, name="scanner-lease-renewal", daemon=True
        )
        self._thread.start()

    def _heartbeat(self):
        while not self._stop.wait(self.renew_seconds):
            try:
                row = self.store._run(
                    """UPDATE scanner_leases
                          SET lease_until=now() + (%s * interval '1 second'),
                              updated_at=now()
                        WHERE lease_key=%s AND owner_token=%s
                          AND lease_until > now()
                    RETURNING owner_token""",
                    (self.lease_seconds, self.LEASE_KEY, self.owner),
                    "one",
                )
            except Exception:
                # Keep retrying through a transient DB outage. The database
                # expiry bounds how long another process must wait.
                log.exception("scanner lease renewal failed")
                continue
            if not row or row[0] != self.owner:
                self._lost.set()
                self.acquired = False
                log.error("scanner leader lease was lost")
                return

    def release(self):
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(1, self.renew_seconds + 1))
        if self.acquired and getattr(self.store, "kind", "") == "postgres":
            try:
                self.store._run(
                    "DELETE FROM scanner_leases WHERE lease_key=%s AND owner_token=%s",
                    (self.LEASE_KEY, self.owner),
                )
            except Exception:
                # Do not mask the scan result. The row expires automatically.
                log.exception("scanner lease release failed; it will expire")
        self.acquired = False
