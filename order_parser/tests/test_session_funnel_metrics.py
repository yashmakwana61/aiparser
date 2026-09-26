from datetime import datetime, timedelta, timezone

import pytest

from order_parser.core.metrics import REGISTRY
from order_parser.sessions.manager import SessionManager
from order_parser.sessions.models import SessionStatus, StaffIdentity
from order_parser.sessions.store import SessionStore
from order_parser.tests.test_session_store import make_session


@pytest.fixture(autouse=True)
def clean_registry():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


def transitions(**labels) -> float:
    return REGISTRY.counter_value("session_status_transitions_total", **labels)


BOB = StaffIdentity(telegram_user_id=111, staff_id="bob", display_name="Bob")


def test_start_and_finish_record_transitions(tmp_path):
    manager = SessionManager(SessionStore(tmp_path / "sessions"))
    session = manager.start_session(identity=BOB, chat_id=555)
    assert transitions(status_from="NEW", status_to="COLLECTING") == 1

    manager.add_text(session.session_id, staff_id="bob", text="two widgets please")
    manager.finish(session.session_id, staff_id="bob")
    assert transitions(status_from="COLLECTING", status_to="PROCESSING") == 1


def test_cancel_records_transition(tmp_path):
    manager = SessionManager(SessionStore(tmp_path / "sessions"))
    session = manager.start_session(identity=BOB, chat_id=555)

    manager.cancel(session.session_id, staff_id="bob")
    assert transitions(status_from="COLLECTING", status_to="CANCELLED") == 1


def test_expiry_records_transition(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    stale = make_session("ses_stale01")
    stale.status = SessionStatus.PROCESSING
    stale.updated_at = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat().replace("+00:00", "Z")
    store.save(stale)

    manager = SessionManager(store, timeout_minutes=60)
    expired = manager.expire_stale()

    assert [s.session_id for s in expired] == ["ses_stale01"]
    assert transitions(status_from="PROCESSING", status_to="EXPIRED") == 1
