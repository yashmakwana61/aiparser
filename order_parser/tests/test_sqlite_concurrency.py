import sqlite3
import threading

import pytest

from order_parser.core.email_state_store import EmailStateStore
from order_parser.core.idempotency_store import IdempotencyStore


def _journal_mode(path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()
    finally:
        conn.close()


# --------------------------------------------------------------- pragmas set


def test_idempotency_store_uses_wal(tmp_path):
    store = IdempotencyStore(tmp_path / "idem.sqlite3")
    assert store._connect() is not None
    assert _journal_mode(store.path) == "wal"


def test_email_state_store_uses_wal(tmp_path):
    store = EmailStateStore(tmp_path / "state.sqlite3")
    assert store._connect() is not None
    assert _journal_mode(store.path) == "wal"


def test_busy_timeout_configured(tmp_path):
    store = IdempotencyStore(tmp_path / "idem.sqlite3")
    conn = store._connect()
    assert conn is not None
    # python sqlite3 timeout maps to the busy handler (ms in the pragma).
    busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert int(busy) >= 30_000


# --------------------------------------------------- cross-connection writers


def test_two_stores_same_db_concurrent_writes(tmp_path):
    """Simulates CLI + server processes writing the same database.

    Separate IdempotencyStore instances mean separate connections; with the
    default rollback journal this intermittently raises 'database is
    locked'. With WAL + busy_timeout every write lands.
    """
    db_path = tmp_path / "shared.sqlite3"
    stores = [IdempotencyStore(db_path) for _ in range(4)]
    errors: list[Exception] = []
    recorded: list[tuple] = []

    def worker(store, prefix: str):
        for i in range(25):
            fingerprint = f"{prefix}-{i:03d}"
            try:
                store.record(fingerprint=fingerprint, source="telegram")
            except Exception as exc:  # pragma: no cover - failure signal only
                errors.append(exc)
                return

    threads = [
        threading.Thread(target=worker, args=(store, f"fp{n}"))
        for n, store in enumerate(stores)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []

    check = IdempotencyStore(db_path)
    for n in range(4):
        for i in range(25):
            assert check.find_recent(f"fp{n}-{i:03d}") is not None


def test_record_roundtrip_smoke(tmp_path):
    store = IdempotencyStore(tmp_path / "idem.sqlite3")
    store.record(fingerprint="abc", source="email")
    assert store.find_recent("abc") is not None
    assert store.find_recent("missing") is None
