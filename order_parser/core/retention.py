from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from order_parser.core import metrics
from order_parser.sessions.models import ACTIVE_STATUSES
from order_parser.sessions.store import SessionStore

logger = structlog.get_logger(__name__)


@dataclass
class RetentionResult:
    sessions_pruned: int = 0
    corrupt_files_pruned: int = 0
    idempotency_rows_purged: int = 0
    audit_files_pruned: int = 0
    bytes_freed: int = 0


@dataclass
class PrunableSession:
    session_id: str
    status: str
    updated_at: str
    bytes: int


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _dir_size(path) -> int:
    total = 0
    for file in path.rglob("*"):
        try:
            if file.is_file():
                total += file.stat().st_size
        except OSError:
            continue
    return total


def collect_prunable_sessions(
    store: SessionStore, retention_days: int, now: datetime | None = None
) -> list[PrunableSession]:
    """Sessions the sweeper would delete, without deleting anything.

    Same rules as :func:`prune_sessions`: active statuses never qualify,
    unparseable timestamps are kept (fail safe), and the boundary is
    ``updated_at >= cutoff`` stays.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(0, int(retention_days)))
    candidates: list[PrunableSession] = []
    for session in store.list_all():
        if session.status in ACTIVE_STATUSES:
            continue
        updated = _parse_iso(session.updated_at)
        if updated is None or updated >= cutoff:
            continue
        attachments_path = store.directory / "attachments" / session.session_id
        size = _dir_size(attachments_path)
        session_file = store.directory / f"{session.session_id}.json"
        try:
            size += session_file.stat().st_size
        except OSError:
            pass
        candidates.append(
            PrunableSession(
                session_id=session.session_id,
                status=session.status.value,
                updated_at=session.updated_at,
                bytes=size,
            )
        )
    return candidates


def _prunable_corrupt_files(store: SessionStore, cutoff: datetime) -> list:
    return [
        path
        for path in store.directory.glob("*.json.corrupt-*")
        if path.stat().st_mtime <= cutoff.timestamp()
    ]


def prune_sessions(store: SessionStore, retention_days: int, now: datetime | None = None) -> RetentionResult:
    """Delete terminal sessions older than the retention window.

    Sessions in any active status are never deleted, no matter how old —
    only COMPLETED / FAILED / CANCELLED / EXPIRED work is reclaimed.
    Unparseable timestamps keep the file (fail safe: we do not delete what
    we cannot date). Orphaned ``*.json.corrupt-*`` quarantine files from the
    session store's corruption handling are pruned on the same clock.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(0, int(retention_days)))
    result = RetentionResult()

    for candidate in collect_prunable_sessions(store, retention_days, now=now):
        freed = candidate.bytes
        attachments_path = store.directory / "attachments" / candidate.session_id
        if not store.delete(candidate.session_id):
            continue
        shutil.rmtree(attachments_path, ignore_errors=True)
        result.sessions_pruned += 1
        result.bytes_freed += freed

    for path in _prunable_corrupt_files(store, cutoff):
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        result.corrupt_files_pruned += 1
        result.bytes_freed += size

    metrics.incr("retention_sessions_pruned_total", value=result.sessions_pruned)
    metrics.incr("retention_corrupt_files_pruned_total", value=result.corrupt_files_pruned)
    metrics.incr("retention_bytes_freed_total", value=result.bytes_freed)
    return result


def run_retention_sweep(
    store: SessionStore,
    idempotency_store=None,
    retention_days: int = 30,
    now: datetime | None = None,
    audit_retention_days: int = 0,
    audit_directory: str | None = None,
) -> RetentionResult:
    """One full retention pass: sessions/attachments + idempotency history.

    Audit day-file pruning only runs when ``audit_retention_days`` is > 0
    (default keeps the audit archive forever).
    """
    result = prune_sessions(store, retention_days, now=now)
    if idempotency_store is not None:
        try:
            result.idempotency_rows_purged = idempotency_store.purge(older_than_days=retention_days)
        except Exception:
            logger.exception("retention.idempotency_purge_failed")
    if audit_retention_days > 0:
        try:
            from order_parser.core.audit import prune_audit_days

            result.audit_files_pruned = prune_audit_days(
                audit_retention_days, directory=audit_directory, now=now
            )
        except Exception:
            logger.exception("retention.audit_prune_failed")
    if (
        result.sessions_pruned
        or result.corrupt_files_pruned
        or result.idempotency_rows_purged
        or result.audit_files_pruned
    ):
        logger.info(
            "retention.swept",
            sessions=result.sessions_pruned,
            corrupt_files=result.corrupt_files_pruned,
            history_rows=result.idempotency_rows_purged,
            audit_files=result.audit_files_pruned,
            bytes_freed=result.bytes_freed,
        )
    return result
