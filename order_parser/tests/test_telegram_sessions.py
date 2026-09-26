"""End-to-end Telegram session flows with fakes (no network, no PTB runtime)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from order_parser.channels.telegram_handler import TelegramHandler
from order_parser.models import ParsedOrder  # noqa: F401 (used via make_parsed)
from order_parser.services.session_service import SessionService
from order_parser.sessions.manager import SessionManager
from order_parser.sessions.staff_registry import StaffRegistry
from order_parser.sessions.store import SessionStore
from order_parser.tests.test_session_service import FakePipeline, make_parsed


# --------------------------------------------------------------------- fakes


class FakeUser:
    def __init__(self, user_id: int, name: str = "Bob"):
        self.id = user_id
        self.full_name = name


class FakeMessage:
    def __init__(self, text: str = "", user_id: int = 111, chat_id: int = 555):
        self.text = text or None
        self.caption = ""
        self.chat_id = chat_id
        self.message_id = 1000
        self.from_user = FakeUser(user_id)
        self.photo = []
        self.document = None
        self.replies: list[str] = []
        self.markups: list[object] = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)
        self.markups.append(reply_markup)


class FakeQuery:
    def __init__(self, data: str, user_id: int = 111):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.answered: list[tuple] = []
        self.message = FakeMessage()

    async def answer(self, text=None, show_alert=False):
        self.answered.append((text, show_alert))


def make_update(message: FakeMessage | None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_message=message,
        effective_user=message.from_user if message else None,
        callback_query=None,
    )


def make_update_callback(query: FakeQuery) -> SimpleNamespace:
    return SimpleNamespace(
        effective_message=query.message,
        effective_user=query.from_user,
        callback_query=query,
    )


def build_handler(tmp_path, pipeline: FakePipeline, registry_raw: str = "111:bob_ops"):
    registry = StaffRegistry(registry_raw)
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, timeout_minutes=60)
    service = SessionService(
        pipeline,
        processors=SimpleNamespace(
            text=SimpleNamespace(process=lambda value: make_parsed("ABC", [("Bread", 20, "Units")])),
            image=None,
            pdf=None,
            excel=None,
        ),
    )
    handler = TelegramHandler(pipeline, session_manager=manager, staff_registry=registry, session_service=service)
    return handler, manager, store


# ---------------------------------------------------------------------- tests


def test_unauthorized_user_rejected_without_processing(tmp_path):
    pipeline = FakePipeline()
    handler, _, _ = build_handler(tmp_path, pipeline)
    message = FakeMessage("bread 20", user_id=999)

    asyncio.run(handler.handle_update(make_update(message), None))

    assert any("not authorized" in reply for reply in message.replies)
    assert pipeline.calls == []


def test_neworder_then_capture_then_done_pends_with_buttons(tmp_path):
    pipeline = FakePipeline()
    pipeline.result = {"status": "pending", "order_id": "ord123", "confidence": 85}
    handler, manager, _ = build_handler(tmp_path, pipeline)

    start = FakeMessage("/neworder")
    asyncio.run(handler.handle_update(make_update(start), None))
    assert any("New order started" in r for r in start.replies)

    session = manager.get_collecting_session("bob_ops")
    assert session is not None

    first = FakeMessage("ABC Industries", user_id=111)
    second = FakeMessage("bread 20", user_id=111)
    asyncio.run(handler.handle_update(make_update(first), None))
    asyncio.run(handler.handle_update(make_update(second), None))
    # capture mode: pipeline must NOT run per message
    assert pipeline.calls == []

    stored = manager.get(session.session_id)
    assert len(stored.messages) == 2

    done = FakeMessage("/done", user_id=111)
    asyncio.run(handler.handle_update(make_update(done), None))
    assert len(pipeline.calls) == 1
    source, input_type, parsed, raw = pipeline.calls[0]
    assert input_type == "session" and raw["session_id"] == session.session_id
    final = manager.get(session.session_id)
    assert final.status.value == "WAITING_CONFIRMATION"
    assert final.confirmation_state["pending_order_id"] == "ord123"
    assert any("ready for confirmation" in r for r in done.replies)


def test_finish_button_and_confirm_button_complete_session(tmp_path):
    pipeline = FakePipeline()
    pipeline.result = {"status": "pending", "order_id": "ord9", "confidence": 90}
    pipeline.confirm_result = {"status": "success", "sales_order": "SO00077"}
    handler, manager, _ = build_handler(tmp_path, pipeline)

    start = FakeMessage("/neworder")
    asyncio.run(handler.handle_update(make_update(start), None))
    item = FakeMessage("milk 5", user_id=111)
    asyncio.run(handler.handle_update(make_update(item), None))

    finish_btn = FakeQuery("sess:finish")
    asyncio.run(handler.handle_callback(make_update_callback(finish_btn), None))
    assert finish_btn.answered
    assert manager.get_collecting_session("bob_ops") is None

    confirm_btn = FakeQuery("sess:confirm")
    asyncio.run(handler.handle_callback(make_update_callback(confirm_btn), None))

    stored = manager.store.list_all()
    assert len(stored) == 1
    final = stored[0]
    assert final.status.value == "COMPLETED"
    assert final.odoo_sale_order_name == "SO00077"
    assert any("SO00077" in r for r in confirm_btn.message.replies)


def test_cancel_command_terminates_session(tmp_path):
    pipeline = FakePipeline()
    handler, manager, _ = build_handler(tmp_path, pipeline)
    asyncio.run(handler.handle_update(make_update(FakeMessage("/neworder")), None))
    cancel = FakeMessage("/cancel", user_id=111)
    asyncio.run(handler.handle_update(make_update(cancel), None))
    assert manager.get_latest_for_staff("bob_ops") is None
    assert any("cancelled" in r for r in cancel.replies)


def test_status_command_reports_counts(tmp_path):
    pipeline = FakePipeline()
    handler, manager, _ = build_handler(tmp_path, pipeline)
    asyncio.run(handler.handle_update(make_update(FakeMessage("/neworder")), None))
    status = FakeMessage("/status", user_id=111)
    asyncio.run(handler.handle_update(make_update(status), None))
    joined = "\n".join(status.replies)
    assert "COLLECTING" in joined and "Session" in joined


def test_legacy_single_shot_still_works_without_session(tmp_path):
    pipeline = FakePipeline()
    pipeline.result = {"status": "success", "sales_order": "SO001"}
    handler, manager, _ = build_handler(tmp_path, pipeline)
    # legacy text path uses the handler's built-in TextProcessor; replace it
    handler.text_processor = SimpleNamespace(process=lambda value: make_parsed("Legacy Co"))
    message = FakeMessage("quick order for Legacy Co two bread", user_id=111)
    asyncio.run(handler.handle_update(make_update(message), None))
    assert len(pipeline.calls) == 1
    assert any("SO001" in r for r in message.replies)


def test_empty_session_cannot_be_finished(tmp_path):
    pipeline = FakePipeline()
    handler, _, _ = build_handler(tmp_path, pipeline)
    asyncio.run(handler.handle_update(make_update(FakeMessage("/neworder")), None))
    done = FakeMessage("/done", user_id=111)
    asyncio.run(handler.handle_update(make_update(done), None))
    assert pipeline.calls == []
    assert any("empty" in r.lower() for r in done.replies)


def test_conflict_warnings_surface_in_reply(tmp_path):
    pipeline = FakePipeline()

    def text_two_fragments(value):
        if "20" in value:
            return make_parsed("ABC", [("Bread", 20, "Units")], confidence=98)
        return make_parsed("", [("Bread", 25, "Units")], confidence=98)

    handler, manager, _ = build_handler(tmp_path, pipeline)
    handler.session_service = SessionService(
        pipeline,
        processors=SimpleNamespace(
            text=SimpleNamespace(process=text_two_fragments), image=None, pdf=None, excel=None
        ),
    )
    asyncio.run(handler.handle_update(make_update(FakeMessage("/neworder")), None))
    asyncio.run(handler.handle_update(make_update(FakeMessage("bread 20", user_id=111)), None))
    asyncio.run(handler.handle_update(make_update(FakeMessage("bread 25", user_id=111)), None))
    pipeline.result = {"status": "auto_blocked_never", "order_id": "x"}  # would be pending path anyway
    pipeline.result = {"status": "pending", "order_id": "ordC", "confidence": 94}
    done = FakeMessage("/done", user_id=111)
    asyncio.run(handler.handle_update(make_update(done), None))
    joined = "\n".join(done.replies)
    assert "Conflict" in joined


# ------------------------------------------------------- review-blocked flow


def _run_review_order(tmp_path, registry_raw="111:bob_ops"):
    pipeline = FakePipeline()
    pipeline.result = {
        "status": "review",
        "confidence": 100,
        "resolution_blocked": ["PRODUCT_AMBIGUOUS: item 4", "TAX_UNRESOLVED: items 1-4"],
    }
    handler, manager, _ = build_handler(tmp_path, pipeline, registry_raw=registry_raw)

    start = FakeMessage("/neworder")
    asyncio.run(handler.handle_update(make_update(start), None))
    session = manager.get_collecting_session("bob_ops")
    item = FakeMessage("bread white packet 700g x10", user_id=111)
    asyncio.run(handler.handle_update(make_update(item), None))
    done = FakeMessage("/done", user_id=111)
    asyncio.run(handler.handle_update(make_update(done), None))
    return handler, manager, session, done


def test_review_outcome_attaches_why_keyboard(tmp_path):
    handler, manager, session, done = _run_review_order(tmp_path)

    final = manager.get(session.session_id)
    assert final.status.value == "FAILED"
    assert final.error_state["code"] == "REVIEW_REQUIRED"

    keyboards = [m for m in done.markups if m is not None]
    assert keyboards, "review reply must carry a keyboard"
    buttons = [btn.callback_data for row in keyboards[-1].inline_keyboard for btn in row]
    assert f"sess:why:{session.session_id}" in buttons
    assert "sess:cancel" in buttons
    # No confirm button on blocked orders — they usually cannot be created.
    assert "sess:confirm" not in buttons
    assert any("manual review" in r for r in done.replies)


def test_why_button_explains_blocking_reasons(tmp_path):
    handler, manager, session, _ = _run_review_order(tmp_path)

    why_btn = FakeQuery(f"sess:why:{session.session_id}")
    asyncio.run(handler.handle_callback(make_update_callback(why_btn), None))

    assert why_btn.answered
    text = "\n".join(why_btn.message.replies)
    assert "needs manual review" in text
    assert "PRODUCT_AMBIGUOUS" in text
    assert "TAX_UNRESOLVED" in text
    assert "/neworder" in text


def test_why_button_rejects_other_staffs_session(tmp_path):
    handler, manager, session, _ = _run_review_order(
        tmp_path, registry_raw="111:bob_ops,222:alice"
    )

    outsider = FakeQuery(f"sess:why:{session.session_id}", user_id=222)
    asyncio.run(handler.handle_callback(make_update_callback(outsider), None))

    text = "\n".join(outsider.message.replies)
    assert "different staff member" in text
    assert "PRODUCT_AMBIGUOUS" not in text


def test_why_button_unknown_session_falls_back_gracefully(tmp_path):
    handler, _, _, _ = _run_review_order(tmp_path)

    stale = FakeQuery("sess:why:doesnotexist99")
    asyncio.run(handler.handle_callback(make_update_callback(stale), None))

    assert stale.answered
    text = "\n".join(stale.message.replies)
    assert "No blocking details" in text or "needs manual review" in text
