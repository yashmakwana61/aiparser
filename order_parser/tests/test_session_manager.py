import pytest

from order_parser.sessions.manager import (
    SessionAccessError,
    SessionActiveError,
    SessionError,
    SessionManager,
)
from order_parser.sessions.models import (
    SessionAttachment,
    SessionStatus,
    StaffIdentity,
    StaffSession,
)
from order_parser.sessions.staff_registry import StaffRegistry
from order_parser.sessions.store import SessionStore


def make_manager(tmp_path, timeout_minutes: int = 60) -> SessionManager:
    return SessionManager(SessionStore(tmp_path / "sessions"), timeout_minutes=timeout_minutes)


def bob() -> StaffIdentity:
    return StaffIdentity(telegram_user_id=111, staff_id="bob", display_name="Bob")


def test_start_session_creates_collecting(tmp_path):
    manager = make_manager(tmp_path)
    session = manager.start_session(bob(), chat_id=42)
    assert session.status == SessionStatus.COLLECTING
    assert session.chat_id == 42
    assert manager.get(session.session_id).staff_id == "bob"


def test_second_active_session_rejected(tmp_path):
    manager = make_manager(tmp_path)
    first = manager.start_session(bob())
    with pytest.raises(SessionActiveError) as exc:
        manager.start_session(bob())
    assert exc.value.session.session_id == first.session_id


def test_add_text_and_attachment(tmp_path):
    manager = make_manager(tmp_path)
    session = manager.start_session(bob())
    manager.add_text(session.session_id, "bob", "ABC Industries", telegram_message_id=7)
    attachment = SessionAttachment(
        kind="photo",
        input_type="image",
        filename="a.jpg",
        path="/tmp/a.jpg",
        sha256="deadbeef",
        size_bytes=10,
    )
    updated = manager.add_attachment(session.session_id, "bob", attachment)
    assert len(updated.messages) == 1
    assert len(updated.attachments) == 1
    assert isinstance(updated.updated_at, str) and updated.updated_at


def test_duplicate_attachment_by_hash_ignored(tmp_path):
    manager = make_manager(tmp_path)
    session = manager.start_session(bob())
    attachment = SessionAttachment(
        kind="photo", input_type="image", filename="a.jpg", path="/tmp/a.jpg",
        sha256="cafe01", size_bytes=5,
    )
    manager.add_attachment(session.session_id, "bob", attachment)
    again = manager.add_attachment(session.session_id, "bob", attachment)
    assert len(again.attachments) == 1


def test_finish_requires_content_then_transitions(tmp_path):
    manager = make_manager(tmp_path)
    session = manager.start_session(bob())
    with pytest.raises(SessionError):
        manager.finish(session.session_id, "bob")
    manager.add_text(session.session_id, "bob", "bread 20")
    finished = manager.finish(session.session_id, "bob")
    assert finished.status == SessionStatus.PROCESSING
    with pytest.raises(SessionError):  # PROCESSING -> PROCESSING illegal
        manager.finish(session.session_id, "bob")


def test_cancel_is_terminal(tmp_path):
    manager = make_manager(tmp_path)
    session = manager.start_session(bob())
    cancelled = manager.cancel(session.session_id, "bob")
    assert cancelled.status == SessionStatus.CANCELLED
    with pytest.raises(SessionError):
        manager.add_text(session.session_id, "bob", "more")


def test_ownership_enforced(tmp_path):
    manager = make_manager(tmp_path)
    session = manager.start_session(bob())
    with pytest.raises(SessionAccessError):
        manager.add_text(session.session_id, "alice", "intruder")
    with pytest.raises(SessionAccessError):
        manager.cancel(session.session_id, "alice")


def test_expire_stale_only_old_sessions(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, timeout_minutes=60)
    old = manager.start_session(bob(), chat_id=1)
    stale_at = datetime.now(timezone.utc) - timedelta(minutes=120)
    old.updated_at = stale_at.isoformat()
    store.save(old)

    fresh_identity = StaffIdentity(telegram_user_id=222, staff_id="amy", display_name="Amy")
    fresh = manager.start_session(fresh_identity)

    expired = manager.expire_stale()
    assert [s.session_id for s in expired] == [old.session_id]
    assert manager.get(old.session_id).status == SessionStatus.EXPIRED
    assert manager.get(fresh.session_id).status == SessionStatus.COLLECTING


def test_registry_parsing_and_resolution():
    registry = StaffRegistry("111:bob_ops, 222:Alice Sales;bad-entry,333:")
    assert registry.enforced is True
    resolved = registry.resolve(111)
    assert resolved is not None and resolved.staff_id == "bob_ops"
    assert registry.resolve(222).staff_id == "Alice Sales"
    assert registry.resolve(999) is None
    assert registry.resolve(None) is None


def test_registry_empty_disables_enforcement():
    registry = StaffRegistry("")
    assert registry.enforced is False
