from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import structlog

from order_parser.config import get_settings
from order_parser.utils import ensure_directory

logger = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_messages (
    message_key TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT '',
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_time ON seen_messages (seen_at);
"""


class EmailStateStore:
    """SQLite-backed record of already-ingested emails (Phase 9).

    Keyed by normalized Message-ID (or content hash when absent) so a
    re-delivered or re-marked-unread email is never processed twice - even
    across restarts, unlike \\Seen flags which other clients can change.
    All methods degrade gracefully on database trouble.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or getattr(get_settings(), "email_state_db_path", "")
        self.path = Path(configured) if configured else Path(get_settings().log_dir) / "email_state.sqlite3"
        ensure_directory(self.path.parent)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        try:
            # WAL + busy_timeout: see idempotency_store._connect.
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
            return conn
        except sqlite3.Error:
            logger.warning("email_state.connect_failed", path=str(self.path))
            return None

    def _drop_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def already_seen(self, message_key: str, window_days: int = 7) -> bool:
        if not message_key:
            return False
        cutoff = utc_iso(days_ago=max(0, int(window_days)))
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return False
                row = conn.execute(
                    "SELECT 1 FROM seen_messages WHERE message_key = ? AND seen_at >= ? LIMIT 1",
                    (message_key, cutoff),
                ).fetchone()
                return bool(row)
        except sqlite3.Error:
            logger.exception("email_state.read_failed")
            self._drop_connection()
            return False

    def mark_seen(self, message_key: str, source: str = "", seen_at: str | None = None) -> None:
        if not message_key:
            return
        stamp = seen_at or utc_iso()
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return
                conn.execute(
                    "INSERT OR REPLACE INTO seen_messages (message_key, source, seen_at) VALUES (?, ?, ?)",
                    (message_key, source, stamp),
                )
                conn.commit()
        except sqlite3.Error:
            logger.exception("email_state.write_failed")
            self._drop_connection()

    def purge(self, older_than_days: int = 30) -> int:
        cutoff = utc_iso(days_ago=max(1, int(older_than_days)))
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return 0
                cursor = conn.execute("DELETE FROM seen_messages WHERE seen_at < ?", (cutoff,))
                conn.commit()
                return cursor.rowcount
        except sqlite3.Error:
            logger.exception("email_state.purge_failed")
            self._drop_connection()
            return 0

    def count(self) -> int:
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return 0
                return int(conn.execute("SELECT COUNT(*) FROM seen_messages").fetchone()[0])
        except sqlite3.Error:
            logger.exception("email_state.count_failed")
            self._drop_connection()
            return 0


def utc_iso(days_ago: int = 0) -> str:
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.isoformat()
