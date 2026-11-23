import threading
import time

from app.scanner_lease import ScannerLease


class MemoryStore:
    kind = "memory"


class FakePostgresStore:
    kind = "postgres"

    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None
        self.expires_at = 0.0
        self.table_created = False

    def _run(self, sql, params=None, fetch="none"):
        params = params or ()
        with self.lock:
            if sql.lstrip().startswith("CREATE TABLE"):
                self.table_created = True
                return None
            if "INSERT INTO scanner_leases" in sql:
                key, owner, ttl = params
                now = time.monotonic()
                if self.owner is None or self.expires_at <= now or self.owner == owner:
                    self.owner = owner
                    self.expires_at = now + int(ttl)
                    return (owner,) if fetch == "one" else None
                return None
            if "UPDATE scanner_leases" in sql:
                ttl, key, owner = params
                if self.owner == owner and self.expires_at > time.monotonic():
                    self.expires_at = time.monotonic() + int(ttl)
                    return (owner,) if fetch == "one" else None
                return None
            if "DELETE FROM scanner_leases" in sql:
                key, owner = params
                if self.owner == owner:
                    self.owner = None
                    self.expires_at = 0.0
                return None
        raise AssertionError(f"Unexpected SQL: {sql}")


def test_memory_store_lease_is_noop_for_single_process_tests():
    lease = ScannerLease(MemoryStore())
    assert lease.acquire() is True
    lease.start_heartbeat()
    lease.release()
    assert lease.acquired is False


def test_postgres_lease_allows_only_one_scanner_owner():
    store = FakePostgresStore()
    first = ScannerLease(store)
    second = ScannerLease(store)

    assert first.acquire() is True
    assert second.acquire() is False
    assert store.table_created is True

    first.release()
    assert second.acquire() is True
    second.release()


def test_expired_lease_can_be_taken_over_and_old_owner_cannot_release_new_owner():
    store = FakePostgresStore()
    old = ScannerLease(store, lease_seconds=1)
    new = ScannerLease(store)

    assert old.acquire() is True
    time.sleep(1.05)
    assert new.acquire() is True

    old.release()
    assert store.owner == new.owner
    new.release()


def test_database_error_fails_closed():
    class BrokenStore:
        kind = "postgres"

        def _run(self, *args, **kwargs):
            raise RuntimeError("database unavailable")

    assert ScannerLease(BrokenStore()).acquire() is False


def test_heartbeat_renews_lease_during_long_scan():
    store = FakePostgresStore()
    first = ScannerLease(store, lease_seconds=1, renew_seconds=0.1)
    second = ScannerLease(store, lease_seconds=1, renew_seconds=0.1)

    assert first.acquire() is True
    first.start_heartbeat()
    time.sleep(1.25)
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()
