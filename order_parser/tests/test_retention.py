"""Phase 13 hardening: retention sweeper for sessions, attachments, quarantine."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from order_parser.core import metrics
from order_parser.core.metrics import REGISTRY
from order_parser.core.retention import prune_sessions, run_retention_sweep
from order_parser.sessions.models import SessionStatus
from order_parser.sessions.store import SessionStore
from order_parser.tests.test_session_store import make_session


NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def aged_session(session_id: str, days_ago: float, status: SessionStatus) -> object:
    session = make_session(session_id)
    session.status = status
    session.updated_at = iso(days_ago)
    return session


@pytest.fixture(autouse=True)
def _isolate():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


def seed_attachments(store: SessionStore, session_id: str, files: int = 2) -> int:
    """Create attachment payload bytes; returns total size."""
    att_dir = store.directory / "attachments" / session_id
    att_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for i in range(files):
        target = att_dir / f"file{i}.bin"
        payload = b"x" * 100
        target.write_bytes(payload)
        total += len(payload)
    return total


# ----------------------------------------------------------------- pruning


@pytest.mark.usefixtures("_isolate")
def test_prune_removes_old_terminal_sessions(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.save(aged_session("ses_oldfail", days_ago=45, status=SessionStatus.FAILED))
    store.save(aged_session("ses_oldcomp", days_ago=40, status=SessionStatus.COMPLETED))

    result = prune_sessions(store, retention_days=30, now=NOW)

    assert result.sessions_pruned == 2
    assert store.list_all() == []
    assert REGISTRY.counter_value("retention_sessions_pruned_total") == 2


@pytest.mark.usefixtures("_isolate")
def test_active_sessions_never_deleted_regardless_of_age(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.save(aged_session("ses_collect", days_ago=400, status=SessionStatus.COLLECTING))
    store.save(aged_session("ses_waiting", days_ago=365, status=SessionStatus.WAITING_CONFIRMATION))

    result = prune_sessions(store, retention_days=30, now=NOW)

    assert result.sessions_pruned == 0
    assert {s.session_id for s in store.list_all()} == {"ses_collect", "ses_waiting"}


@pytest.mark.usefixtures("_isolate")
def test_recent_terminal_session_is_kept(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.save(aged_session("ses_new", days_ago=1, status=SessionStatus.COMPLETED))

    result = prune_sessions(store, retention_days=30, now=NOW)

    assert result.sessions_pruned == 0
    assert len(store.list_all()) == 1


@pytest.mark.usefixtures("_isolate")
def test_boundary_age_exactly_at_cutoff_is_kept(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.save(aged_session("ses_edge", days_ago=30, status=SessionStatus.CANCELLED))

    result = prune_sessions(store, retention_days=30, now=NOW)

    assert result.sessions_pruned == 0
    assert len(store.list_all()) == 1


@pytest.mark.usefixtures("_isolate")
def test_attachment_directory_removed_and_bytes_counted(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = aged_session("ses_files", days_ago=60, status=SessionStatus.EXPIRED)
    store.save(session)
    payload_bytes = seed_attachments(store, "ses_files")

    result = prune_sessions(store, retention_days=30, now=NOW)

    assert result.sessions_pruned == 1
    assert result.bytes_freed >= payload_bytes
    assert not (store.directory / "attachments" / "ses_files").exists()
    assert not (store.directory / "ses_files.json").exists()
    assert REGISTRY.counter_value("retention_bytes_freed_total") >= payload_bytes


@pytest.mark.usefixtures("_isolate")
def test_unparseable_timestamp_blocks_deletion(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = aged_session("ses_broken", days_ago=90, status=SessionStatus.FAILED)
    session.updated_at = "not-a-timestamp"
    store.save(session)

    result = prune_sessions(store, retention_days=30, now=NOW)

    assert result.sessions_pruned == 0
    assert len(store.list_all()) == 1


# ------------------------------------------------------- quarantine hygiene


@pytest.mark.usefixtures("_isolate")
def test_old_quarantine_files_are_removed(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    corrupt_old = store.directory / "ses_dead01.json.corrupt-aaaa1111"
    corrupt_new = store.directory / "ses_dead02.json.corrupt-bbbb2222"
    corrupt_old.write_text("garbage", encoding="utf-8")
    corrupt_new.write_text("garbage", encoding="utf-8")
    old_ts = (NOW - timedelta(days=60)).timestamp()
    new_ts = NOW.timestamp()
    os.utime(corrupt_old, (old_ts, old_ts))
    os.utime(corrupt_new, (new_ts, new_ts))

    result = prune_sessions(store, retention_days=30, now=NOW)

    assert result.corrupt_files_pruned == 1
    assert not corrupt_old.exists()
    assert corrupt_new.exists()
    assert REGISTRY.counter_value("retention_corrupt_files_pruned_total") == 1


# ------------------------------------------------------------ sweep wrapper


class FakeIdempotency:
    def __init__(self, removed: int = 7):
        self.removed = removed
        self.calls: list[int] = []

    def purge(self, older_than_days: int) -> int:
        self.calls.append(older_than_days)
        return self.removed


@pytest.mark.usefixtures("_isolate")
def test_sweep_combines_sessions_and_history(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.save(aged_session("ses_old1", days_ago=99, status=SessionStatus.FAILED))
    idem = FakeIdempotency(removed=5)

    result = run_retention_sweep(store, idem, retention_days=30, now=NOW)

    assert result.sessions_pruned == 1
    assert result.idempotency_rows_purged == 5
    assert idem.calls == [30]


@pytest.mark.usefixtures("_isolate")
def test_sweep_survives_idempotency_failure(tmp_path):
    class BrokenIdem:
        def purge(self, older_than_days):
            raise RuntimeError("db locked")

    store = SessionStore(tmp_path / "sessions")
    store.save(aged_session("ses_old2", days_ago=99, status=SessionStatus.CANCELLED))

    result = run_retention_sweep(store, BrokenIdem(), retention_days=30, now=NOW)

    assert result.sessions_pruned == 1
    assert result.idempotency_rows_purged == 0


@pytest.mark.usefixtures("_isolate")
def test_sweep_on_empty_store_is_clean_noop(tmp_path):
    store = SessionStore(tmp_path / "sessions")

    result = run_retention_sweep(store, None, retention_days=30, now=NOW)

    assert result.sessions_pruned == 0
    assert result.bytes_freed == 0


# ------------------------------------------------------- dry-run collection


@pytest.mark.usefixtures("_isolate")
def test_collect_prunable_matches_real_prune_and_deletes_nothing(tmp_path):
    from order_parser.core.retention import collect_prunable_sessions

    store = SessionStore(tmp_path / "sessions")
    seed_attachments(store, "ses_old_done")
    store.save(aged_session("ses_old_done", 40, SessionStatus.COMPLETED))
    store.save(aged_session("ses_old_active", 40, SessionStatus.COLLECTING))
    store.save(aged_session("ses_recent", 1, SessionStatus.FAILED))
    corrupt = store.directory / "ses_old_done.json.corrupt-abc"
    corrupt.write_text("{broken")

    candidates = collect_prunable_sessions(store, retention_days=30, now=NOW)

    assert [c.session_id for c in candidates] == ["ses_old_done"]
    assert candidates[0].status == "COMPLETED"
    assert candidates[0].bytes >= 200
    # Dry run deletes nothing.
    assert store.get("ses_old_done") is not None
    assert corrupt.exists()


@pytest.mark.usefixtures("_isolate")
def test_prune_result_matches_collected_candidates(tmp_path):
    from order_parser.core.retention import collect_prunable_sessions

    store = SessionStore(tmp_path / "sessions")
    for i in range(3):
        sid = f"ses_old_{i:02d}"
        seed_attachments(store, sid)
        store.save(aged_session(sid, 40, SessionStatus.CANCELLED))

    expected_bytes = sum(c.bytes for c in collect_prunable_sessions(store, 30, now=NOW))
    result = prune_sessions(store, retention_days=30, now=NOW)

    assert result.sessions_pruned == 3
    assert result.bytes_freed == expected_bytes
