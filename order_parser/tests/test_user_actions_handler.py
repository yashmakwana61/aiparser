"""Telegram case UX end-to-end with fakes (no network, no PTB runtime)."""

import asyncio
from types import SimpleNamespace

from order_parser.channels.telegram_handler import TelegramHandler
from order_parser.core.job_store import JobStore
from order_parser.core.pending_store import PendingStore
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder


class FakeUser:
    def __init__(self, user_id=8751097833, name="Staff"):
        self.id = user_id
        self.full_name = name


class FakeMessage:
    def __init__(self, text="", user_id=8751097833, chat_id=555):
        self.text = text or None
        self.caption = ""
        self.chat_id = chat_id
        self.message_id = 1000
        self.from_user = FakeUser(user_id)
        self.photo = []
        self.document = None
        self.replies = []
        self.markups = []
        self.edits = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)
        self.markups.append(reply_markup)

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


class FakeQuery:
    def __init__(self, data, user_id=8751097833):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.answered = []
        self.message = FakeMessage()

    async def answer(self, text=None, show_alert=False):
        self.answered.append((text, show_alert))


def make_update(message=None, query=None):
    user = None
    if message is not None:
        user = message.from_user
    elif query is not None:
        user = query.from_user
    return SimpleNamespace(effective_message=message or (query.message if query else None),
                           effective_user=user, callback_query=query)


def _parsed():
    return ParsedOrder(order=OrderModel(
        customer=CustomerModel(name="ABC"),
        items=[ItemModel(product_name="Lappy", quantity=2)],
        metadata=MetadataModel(confidence=90.0)))


class StubPipeline:
    def __init__(self, pending_store):
        self.pending_store = pending_store
        self.resolver = None
        self.odoo = None
        self.calls = []

    def process(self, source, input_type, parsed, raw=None):
        from order_parser.models import ParsedOrder as PO

        self.calls.append((source, input_type, parsed, raw))
        raw = raw or {}
        order_id = "pend-1"
        self.pending_store.save({
            "order_id": order_id, "job_id": raw.get("job_id"), "status": "review",
            "source": source,
            "parsed_order": parsed.model_dump() if isinstance(parsed, PO) else {},
            "validation": {
                "customer": {"valid": False, "reason": "ambiguous_customer", "candidates": [
                    {"partner_id": 7, "partner_name": "X Ltd", "score": 100.0},
                    {"partner_id": 8, "partner_name": "X Trading", "score": 99.0}]},
                "products": []},
            "resolution": {"blocking": ["CUSTOMER_AMBIGUOUS"], "blocking_detail": [],
                           "warnings": [], "missing_information": [], "items": []},
            "raw": raw, "corrections": raw.get("corrections") or [],
            "overrides": raw.get("overrides") or {},
            "created_at": "2026-10-04T12:00:00+00:00",
        })
        return {"status": "review", "order_id": order_id, "customer": "ABC",
                "items": 1, "resolution_blocked": ["CUSTOMER_AMBIGUOUS"],
                "customer_detail": {"raw_name": "ABC", "resolved": False,
                                    "partner_id": None, "partner_name": None},
                "message": "Order sent for manual review."}


class StubOdoo:
    def list_uoms(self, limit=20):
        return [{"id": 1, "name": "Units"}]

    def list_sale_taxes(self, limit=20):
        return [{"id": 32, "name": "GST 18%", "amount": 18.0}]


def _handler(tmp_path):
    job_store = JobStore(tmp_path / "jobs")
    pending_store = PendingStore(tmp_path / "pending")
    pipeline = StubPipeline(pending_store)
    pipeline.odoo = StubOdoo()
    handler = TelegramHandler(pipeline, job_store=job_store)
    handler.text_processor = SimpleNamespace(process=lambda value: _parsed())
    return handler, job_store, pending_store


def make_update_callback(query):
    return SimpleNamespace(effective_message=query.message,
                           effective_user=query.from_user,
                           callback_query=query)


def _send_text(handler, text, user_id=8751097833):
    message = FakeMessage(text, user_id=user_id)
    asyncio.run(handler.handle_update(make_update(message), None))
    return message


def test_direct_order_reply_is_case_render_with_buttons(tmp_path):
    handler, job_store, _ = _handler(tmp_path)
    message = _send_text(handler, "order abc bread 2")
    assert message.replies, "bot must reply"
    text = message.replies[-1]
    assert "Action required" in text
    assert "ORD-" in text
    assert "PRODUCT_AMBIGUOUS" not in text and "Traceback" not in text
    buttons = [b.callback_data for row in message.markups[-1].inline_keyboard for b in row]
    assert any(b.startswith("case:ORD-") for b in buttons)


def test_customer_pick_callback_applies_correction(tmp_path):
    handler, job_store, pending_store = _handler(tmp_path)
    _send_text(handler, "order abc")
    job_id = job_store.list()[0].job_id
    query = FakeQuery(f"case:{job_id}:cu::0")
    asyncio.run(handler.handle_callback(make_update(query=query), None))
    assert query.answered
    edited = " ".join(t for t, _kb in query.message.edits)
    assert "Saved" in (query.answered[-1][0] or "") or "saved" in edited.lower() or edited
    record = pending_store.list()[0]
    assert record["parsed_order"]["order"]["customer"]["name"] == "X Ltd"
    corrections = record["corrections"]
    assert corrections and corrections[0]["original_value"] == "ABC"


def test_enter_flow_routes_next_text_as_correction(tmp_path):
    handler, job_store, pending_store = _handler(tmp_path)
    _send_text(handler, "order abc")
    job_id = job_store.list()[0].job_id
    enter_query = FakeQuery(f"case:{job_id}:cue")
    asyncio.run(handler.handle_callback(make_update(query=enter_query), None))
    assert any("Reply" in r for r in enter_query.message.replies)
    # Next free text from the same user is consumed as the correction.
    follow = _send_text(handler, "Xavier Ltd")
    assert any("saved" in r.lower() for r in follow.replies)
    record = pending_store.list()[0]
    assert record["parsed_order"]["order"]["customer"]["name"] == "Xavier Ltd"


def test_stale_completed_case_button(tmp_path):
    from order_parser.core.job import JobStatus

    handler, job_store, _pending = _handler(tmp_path)
    _send_text(handler, "order abc")
    job = job_store.list()[0]
    job.status = JobStatus.COMPLETED
    job_store.save(job)
    query = FakeQuery(f"case:{job.job_id}:cu::0")
    asyncio.run(handler.handle_callback(make_update(query=query), None))
    combined = " ".join(t for t, _kb in query.message.edits)
    assert "already been updated" in combined


def test_unauthorized_user_cannot_touch_case(tmp_path):
    handler, job_store, _pending = _handler(tmp_path)
    _send_text(handler, "order abc", user_id=8751097833)
    job_id = job_store.list()[0].job_id
    query = FakeQuery(f"case:{job_id}:cu::0", user_id=111)
    asyncio.run(handler.handle_callback(make_update(query=query), None))
    assert any("someone else" in r for r in query.message.replies)


def test_invalid_callback_data_is_safe(tmp_path):
    handler, _js, _ps = _handler(tmp_path)
    query = FakeQuery("case:ORD-1:bogus")
    asyncio.run(handler.handle_callback(make_update(query=query), None))
    assert any("couldn't be understood" in r for r in query.message.replies)


def test_status_command_shows_latest_case(tmp_path):
    handler, _js, _ps = _handler(tmp_path)
    _send_text(handler, "order abc")
    status_msg = FakeMessage("/status")
    asyncio.run(handler.handle_update(make_update(status_msg), None))
    joined = "\n".join(status_msg.replies)
    assert "Action required" in joined and "ORD-" in joined


def test_update_exception_never_leaks_internals(tmp_path):
    handler, _js, _ps = _handler(tmp_path)

    def _boom(value):
        raise RuntimeError("db 'secret' connection at /srv/x failed")

    handler.text_processor = SimpleNamespace(process=_boom)
    message = _send_text(handler, "order abc")
    combined = " ".join(message.replies)
    assert "Support reference" in combined
    assert "secret" not in combined and "Traceback" not in combined


# ------------------------------------------- session flow uses the same cases


def _session_handler(tmp_path):
    from order_parser.services.session_service import SessionService
    from order_parser.sessions.manager import SessionManager
    from order_parser.sessions.staff_registry import StaffRegistry
    from order_parser.sessions.store import SessionStore

    job_store = JobStore(tmp_path / "jobs")
    pending_store = PendingStore(tmp_path / "pending")
    pipeline = _JobBackedPipeline(pending_store)
    registry = StaffRegistry("8751097833:bob_ops")
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, timeout_minutes=60)
    service = SessionService(
        pipeline,
        job_store=job_store,
        processors=SimpleNamespace(
            text=SimpleNamespace(process=lambda value: _parsed_session()),
            image=None, pdf=None, excel=None),
    )
    handler = TelegramHandler(pipeline, session_manager=manager,
                              staff_registry=registry, session_service=service,
                              job_store=job_store)
    handler.text_processor = SimpleNamespace(process=lambda value: _parsed_session())
    return handler, manager


class _JobBackedPipeline:
    """Mimics production: pipeline.process persists a review pending record."""

    def __init__(self, pending_store):
        self.pending_store = pending_store
        self.resolver = None
        self.odoo = StubOdoo()
        self.settings = SimpleNamespace(auto_create_threshold=95.0, confirm_threshold=80.0,
                                        auto_create_customers=False, auto_create_all_orders=False)

    def process(self, source, input_type, parsed, raw=None):
        from order_parser.models import ParsedOrder as PO

        raw = raw or {}
        self.pending_store.save({
            "order_id": "sess-pend-1", "job_id": raw.get("job_id"), "status": "review",
            "source": source,
            "parsed_order": parsed.model_dump() if isinstance(parsed, PO) else {},
            "validation": {
                "customer": {"valid": False, "reason": "ambiguous_customer", "candidates": [
                    {"partner_id": 7, "partner_name": "X Ltd", "score": 100.0}]},
                "products": []},
            "resolution": {"blocking": ["CUSTOMER_AMBIGUOUS"], "blocking_detail": [],
                           "warnings": [], "missing_information": [], "items": []},
            "raw": raw, "corrections": [], "overrides": {},
            "created_at": "2026-10-04T12:00:00+00:00",
        })
        return {"status": "review", "order_id": "sess-pend-1", "customer": "ABC",
                "items": 1, "confidence": 90.0,
                "resolution_blocked": ["CUSTOMER_AMBIGUOUS"],
                "customer_detail": {"raw_name": "ABC", "resolved": False,
                                    "partner_id": None, "partner_name": None},
                "message": "Order sent for manual review."}

    def confirm_order(self, order_id, actor="api"):
        return {"status": "error", "message": "not configured"}


def _parsed_session():
    from order_parser.tests.test_session_service import make_parsed

    return make_parsed("ABC", [("Bread", 20, "Units")])


def test_session_done_renders_case_actions(tmp_path):
    handler, manager = _session_handler(tmp_path)
    asyncio.run(handler.handle_update(make_update(FakeMessage("/neworder")), None))
    asyncio.run(handler.handle_update(make_update(FakeMessage("bread 20")), None))
    done = FakeMessage("/done")
    asyncio.run(handler.handle_update(make_update(done), None))
    joined = "\n".join(done.replies)
    assert "Action required" in joined and "ORD-" in joined
    buttons = [b.callback_data for row in done.markups[-1].inline_keyboard for b in row]
    assert any(b.startswith("case:ORD-") and ":cu:" in b for b in buttons)


def test_session_correct_button_routes_to_case(tmp_path):
    handler, manager = _session_handler(tmp_path)
    asyncio.run(handler.handle_update(make_update(FakeMessage("/neworder")), None))
    asyncio.run(handler.handle_update(make_update(FakeMessage("bread 20")), None))
    asyncio.run(handler.handle_update(make_update(FakeMessage("/done")), None))
    query = FakeQuery("sess:correct", user_id=8751097833)
    asyncio.run(handler.handle_callback(make_update_callback(query), None))
    joined = "\n".join(query.message.replies)
    assert "Action required" in joined
    assert "later phase" not in joined
