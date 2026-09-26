"""Pipeline guard: OCR/interpretation failures are routed to review and can
never be auto-created - even with AUTO_CREATE_ALL_ORDERS enabled."""
from order_parser.config import Settings
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.services.pipeline import OrderPipeline


class NoopOdoo:
    enabled = False

    def create_partner(self, customer):  # pragma: no cover - must never be called
        raise AssertionError("must never create anything on an OCR failure")


def flagged_parsed() -> ParsedOrder:
    return ParsedOrder(
        order=OrderModel(
            customer=CustomerModel(name="ABC"),
            items=[],
            metadata=MetadataModel(source="telegram", input_type="image", confidence=99.0),
        ),
        ai_response={"ocr_failed": True, "error_code": "OCR_UNAVAILABLE"},
    )


def make_pipeline(auto_all: bool) -> OrderPipeline:
    settings = Settings(auto_create_products=False, auto_create_all_orders=auto_all)
    return OrderPipeline(NoopOdoo(), settings=settings)


def test_ocr_failure_routes_to_review():
    pipeline = make_pipeline(auto_all=False)
    result = pipeline.process("telegram", "image", flagged_parsed(), {})
    assert result["status"] == "review"


def test_ocr_failure_cannot_be_overridden_by_auto_create_all():
    pipeline = make_pipeline(auto_all=True)
    result = pipeline.process("telegram", "image", flagged_parsed(), {})
    assert result["status"] == "review"
    assert "sales_order" not in result


def test_normal_image_flow_unchanged_for_valid_orders():
    parsed = ParsedOrder(
        order=OrderModel(
            customer=CustomerModel(name="ABC"),
            items=[ItemModel(product_name="Bread", quantity=20)],
            metadata=MetadataModel(confidence=97.0),
        ),
        ai_response={},
    )
    pipeline = make_pipeline(auto_all=True)
    result = pipeline.process("telegram", "text", parsed, {"chat_id": 1})
    assert result["status"] in ("review", "pending")  # Odoo disabled -> review path, never crash
