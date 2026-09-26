from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import structlog

from order_parser.config import get_settings
from order_parser.utils import ensure_directory

logger = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    order_ref TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_fp_time ON order_history (fingerprint, created_at);
CREATE TABLE IF NOT EXISTS active_claims (
    fingerprint TEXT PRIMARY KEY,
    claimed_at REAL NOT NULL,
    owner TEXT NOT NULL DEFAULT ''
);
"""


class IdempotencyStore:
    """SQLite-backed ingestion history and creation claims.

    Two cooperating mechanisms:

    - ``order_history`` persists the fingerprint of every successfully
      ingested order so duplicates are detected across restarts, including
      auto-created orders that never touch the pending store.
    - ``active_claims`` provides an atomic short-lived claim per fingerprint
      so a retried or concurrent run cannot create the same sales order twice.

    Every method degrades gracefully: if the database is unavailable or
    corrupt the pipeline must keep working exactly as before, so failures are
    logged and answered with safe defaults instead of raised.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or getattr(get_settings(), "idempotency_db_path", "")
        self.path = Path(configured) if configured else Path(get_settings().log_dir) / "idempotency.sqlite3"
        ensure_directory(self.path.parent)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ----------------------------------------------------------------- internal

    def _connect(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        try:
            # WAL + busy_timeout keep short cross-process accesses (ops CLI,
            # ad-hoc queries) from failing with "database is locked".
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
            return conn
        except sqlite3.Error:
            logger.warning("idempotency.connect_failed", path=str(self.path))
            return None

    def _drop_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    # ------------------------------------------------------------------ history

    def record(
        self,
        fingerprint: str,
        status: str = "success",
        order_ref: str = "",
        source: str = "",
        created_at: str | None = None,
    ) -> None:
        """Append an ingestion record (created_at injection is for tests/backfill)."""
        stamp = created_at or datetime_utc_iso()
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return
                conn.execute(
                    "INSERT INTO order_history (fingerprint, status, order_ref, source, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (fingerprint, status, order_ref, source, stamp),
                )
                conn.commit()
        except sqlite3.Error:
            logger.exception("idempotency.record_failed")
            self._drop_connection()

    def find_recent(self, fingerprint: str, window_hours: int = 24) -> dict | None:
        """Most recent history record for the fingerprint inside the window."""
        if not fingerprint:
            return None
        cutoff = datetime_utc_iso(minutes_ago=int(window_hours) * 60)
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return None
                row = conn.execute(
                    "SELECT id, fingerprint, status, order_ref, source, created_at"
                    " FROM order_history WHERE fingerprint = ? AND created_at >= ?"
                    " ORDER BY id DESC LIMIT 1",
                    (fingerprint, cutoff),
                ).fetchone()
        except sqlite3.Error:
            logger.exception("idempotency.find_failed")
            self._drop_connection()
            return None
        if not row:
            return None
        return {
            "id": row[0],
            "fingerprint": row[1],
            "status": row[2],
            "order_ref": row[3],
            "source": row[4],
            "created_at": row[5],
        }

    def purge(self, older_than_days: int = 30) -> int:
        """Delete history rows older than the retention period. Returns count."""
        cutoff = datetime_utc_iso(minutes_ago=max(1, int(older_than_days)) * 24 * 60)
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return 0
                cursor = conn.execute("DELETE FROM order_history WHERE created_at < ?", (cutoff,))
                stale = time.time() - max(3600.0, float(older_than_days) * 86400.0)
                conn.execute("DELETE FROM active_claims WHERE claimed_at < ?", (stale,))
                conn.commit()
                return cursor.rowcount
        except sqlite3.Error:
            logger.exception("idempotency.purge_failed")
            self._drop_connection()
            return 0

    def count(self) -> int:
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return 0
                return int(conn.execute("SELECT COUNT(*) FROM order_history").fetchone()[0])
        except sqlite3.Error:
            logger.exception("idempotency.count_failed")
            self._drop_connection()
            return 0

    # ------------------------------------------------------------------- claims

    def claim(self, fingerprint: str, ttl_seconds: float = 300.0, owner: str = "") -> bool:
        """Atomically claim the fingerprint for a creation attempt.

        Returns False when another live claim exists; stale claims older than
        ``ttl_seconds`` are reclaimed automatically (crashed worker recovery).
        """
        if not fingerprint:
            return True
        now = time.time()
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return False
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT claimed_at FROM active_claims WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
                if row and (now - float(row[0])) <= float(ttl_seconds):
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(
                    "INSERT OR REPLACE INTO active_claims (fingerprint, claimed_at, owner) VALUES (?, ?, ?)",
                    (fingerprint, now, owner),
                )
                conn.execute("COMMIT")
                return True
        except sqlite3.Error:
            logger.exception("idempotency.claim_failed")
            self._drop_connection()
            return False

    def release(self, fingerprint: str) -> None:
        if not fingerprint:
            return
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return
                conn.execute("DELETE FROM active_claims WHERE fingerprint = ?", (fingerprint,))
                conn.commit()
        except sqlite3.Error:
            logger.exception("idempotency.release_failed")
            self._drop_connection()


def datetime_utc_iso(minutes_ago: int = 0) -> str:
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.isoformat()
