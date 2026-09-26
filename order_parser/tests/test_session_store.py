import json

from order_parser.sessions.models import SessionStatus, StaffIdentity, StaffSession
from order_parser.sessions.store import SessionStore


def make_session(session_id: str = "ses_test01") -> StaffSession:
    return StaffSession(
        session_id=session_id,
        staff_id="bob",
        staff_name="Bob",
        telegram_user_id=111,
        chat_id=555,
    )


def test_save_and_get_roundtrip(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = make_session()
    store.save(session)
    loaded = store.get("ses_test01")
    assert loaded is not None
    assert loaded.staff_id == "bob"
    assert loaded.chat_id == 555
    assert loaded.status == SessionStatus.NEW


def test_get_missing_returns_none(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    assert store.get("nope") is None


def test_delete_removes_file(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = make_session()
    store.save(session)
    assert store.delete("ses_test01") is True
    assert store.get("ses_test01") is None
    assert store.delete("ses_test01") is False


def test_list_active_filters_terminal_statuses(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    active = make_session("ses_a")
    done = make_session("ses_b")
    from order_parser.sessions.manager import SessionManager

    manager = SessionManager(store)
    store.save(active)
    store.save(done)
    manager.advance("ses_b", SessionStatus.EXPIRED)
    ids = [s.session_id for s in store.list_active()]
    assert "ses_a" in ids and "ses_b" not in ids


def test_corrupt_file_is_tolerated(tmp_path):
    directory = tmp_path / "sessions"
    directory.mkdir(parents=True)
    (directory / "bad.json").write_text("{not json", encoding="utf-8")
    store = SessionStore(directory)
    assert store.list_all() == []
    assert store.get("bad") is None


def test_attachment_dir_created_per_session(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    path = store.attachment_dir("ses_x")
    assert path.exists() and path.is_dir()


def test_identity_helper_available(tmp_path):
    identity = StaffIdentity(telegram_user_id=1, staff_id="a", display_name="A")
    assert identity.staff_id == "a"
    # ensure model dumps are JSON safe for storage
    payload = json.dumps(make_session().model_dump(mode="json"))
    assert "session_id" in payload
