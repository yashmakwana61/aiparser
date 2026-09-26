from types import SimpleNamespace

from order_parser.config import Settings
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.services.session_service import SessionService
from order_parser.sessions.models import SessionAttachment, SessionMessage, StaffSession


class FakePipeline:
    def __init__(self):
        self.settings = SimpleNamespace(auto_create_threshold=95.0)
        self.calls = []
        self.result = {"status": "success", "sales_order": "SO00042"}
        self.confirm_result = {"status": "error", "message": "no confirmation configured"}

    def process(self, source, input_type, parsed, raw=None):
        self.calls.append((source, input_type, parsed, raw))
        return dict(self.result)

    def confirm_order(self, order_id, actor="api"):
        result = dict(self.confirm_result)
        result.setdefault("order_id", order_id)
        return result


def make_parsed(customer="", items=None, confidence=90.0):
    return ParsedOrder(
        order=OrderModel(
            customer=CustomerModel(name=customer),
            items=[ItemModel(product_name=n, quantity=q, uom=u) for n, q, u in (items or [])],
            metadata=MetadataModel(source="telegram", input_type="text", confidence=confidence),
        )
    )


def stub_processors(text_results):
    """Text stub pops canned results; other formats raise unless configured."""
    queue = list(text_results)

    class TextStub:
        def process(self, value):
            if not queue:
                raise AssertionError("no canned text results left")
            result = queue.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    def image_stub(data, filename=""):
        raise AssertionError("image processor should not be used in this test")

    return SimpleNamespace(
        text=TextStub(),
        image=image_stub,
        pdf=SimpleNamespace(process=lambda data, filename: (_ for _ in ()).throw(AssertionError("pdf"))),
        excel=SimpleNamespace(process=lambda data, filename: make_parsed("Excel Co", [("Milk", 5, "Box")])),
    )


def make_session() -> StaffSession:
    session = StaffSession(session_id="ses_m", staff_id="bob", chat_id=1)
    session.messages.append(SessionMessage(text="first"))
    session.messages.append(SessionMessage(text="second"))
    return session


def test_customer_priority_and_contact_fill():
    first = make_parsed("ABC Industries")
    second = ParsedOrder(
        order=OrderModel(
            customer=CustomerModel(name="ABC Industries Pvt Ltd", email="ops@abc.com"),
            metadata=MetadataModel(input_type="pdf", confidence=80.0),
        )
    )
    service = SessionService(FakePipeline(), processors=stub_processors([first, second]))
    outcome = service.run(make_session())
    customer = outcome["parsed"].order.customer
    # Phase 5 policy: compatible variant keeps the longest form; contact fills.
    assert customer.name == "ABC Industries Pvt Ltd"
    assert customer.email == "ops@abc.com"


def test_identical_items_collapse():
    first = make_parsed("ABC", [("Bread", 20, "Units")])
    second = make_parsed("", [("bread", 20, "units")])
    service = SessionService(FakePipeline(), processors=stub_processors([first, second]))
    outcome = service.run(make_session())
    items = outcome["parsed"].order.items
    assert len(items) == 1 and float(items[0].quantity) == 20
    assert outcome["forced_confirmation"] is False


def test_conflicting_quantities_force_confirmation():
    first = make_parsed("ABC", [("Bread", 20, "Units")], confidence=98.0)
    second = make_parsed("", [("Bread", 25, "Units")], confidence=98.0)
    pipeline = FakePipeline()
    service = SessionService(pipeline, processors=stub_processors([first, second]))
    outcome = service.run(make_session())
    assert len(outcome["conflict_warnings"]) == 1
    assert outcome["forced_confirmation"] is True
    # deterministic downgrade below the auto-approval band
    assert outcome["parsed"].order.metadata.confidence < 95.0


def test_empty_session_is_fatal():
    service = SessionService(FakePipeline(), processors=stub_processors([]))
    outcome = service.run(StaffSession(session_id="x", staff_id="bob"))
    assert outcome["parsed"] is None
    assert "No usable" in outcome["fatal"]


def test_finalize_routes_through_pipeline_once(tmp_path):
    first = make_parsed("ABC", [("Bread", 20, "Units")])
    pipeline = FakePipeline()
    service = SessionService(pipeline, processors=stub_processors([first]))
    session = StaffSession(session_id="ses_f", staff_id="bob", chat_id=9)
    session.messages.append(SessionMessage(text="only"))
    outcome = service.finalize(session)
    assert len(pipeline.calls) == 1
    source, input_type, parsed, raw = pipeline.calls[0]
    assert source == "telegram" and input_type == "session"
    assert raw["chat_id"] == 9 and raw["session_id"] == "ses_f"
    assert outcome["result"]["status"] == "success"


def test_excel_attachment_extracted_and_caption_merged(tmp_path):
    data_file = tmp_path / "orders.xlsx"
    data_file.write_bytes(b"binary")

    session = StaffSession(session_id="ses_x", staff_id="bob")
    session.attachments.append(
        SessionAttachment(
            kind="document",
            input_type="excel",
            filename="orders.xlsx",
            path=str(data_file),
            sha256="aa11",
            size_bytes=6,
            caption="urgent delivery",
        )
    )
    excel_parsed = make_parsed("Excel Co", [("Milk", 5, "Box")])
    caption_parsed = make_parsed("", [], confidence=0)

    class ExcelStub:
        def process(self, data, filename=""):
            assert data == b"binary"
            return excel_parsed

    class TextStub:
        def process(self, value):
            return caption_parsed

    processors = SimpleNamespace(
        text=TextStub(),
        image=SimpleNamespace(process=lambda d, f="": (_ for _ in ()).throw(AssertionError())),
        pdf=SimpleNamespace(process=lambda d, f="": (_ for _ in ()).throw(AssertionError())),
        excel=ExcelStub(),
    )
    pipeline = FakePipeline()
    service = SessionService(pipeline, processors=processors)
    outcome = service.finalize(session)
    labels = [f["source"] for f in outcome["fragments"]]
    assert any(label.startswith("excel:") for label in labels)
    assert any(label.startswith("caption:") for label in labels)
