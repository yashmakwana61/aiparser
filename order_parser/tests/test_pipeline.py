from order_parser.config import Settings
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.services.pipeline import OrderPipeline


def _pipeline(odoo, **settings_overrides):
    defaults = {"auto_create_products": False, "auto_create_all_orders": False}
    defaults.update(settings_overrides)
    return OrderPipeline(odoo, settings=Settings(**defaults))


class FakeOdoo:
    enabled = True

    def fetch_product_catalog(self):
        return [{"id": 1, "name": "Keyboard"}, {"id": 2, "name": "Mouse"}]

    def find_partner(self, customer):
        if customer.name == "Existing Co":
            return {"id": 42, "name": "Existing Co"}
        return None

    def create_partner(self, customer):
        return 99

    def create_sale_order(self, partner_id, items, notes=""):
        assert partner_id in (42, 99)
        return {"id": 1, "name": "SO00001"}


def _parsed(confidence: float, product: str = "Keyboard", customer: str = "ABC Industries") -> ParsedOrder:
    order = OrderModel(
        customer=CustomerModel(name=customer),
        items=[ItemModel(product_name=product, quantity=2)],
        metadata=MetadataModel(confidence=confidence),
    )
    return ParsedOrder(order=order)


def test_auto_create_at_high_confidence():
    pipeline = _pipeline(FakeOdoo())
    result = pipeline.process("telegram", "text", _parsed(confidence=97))
    assert result["status"] == "success"
    assert result["sales_order"] == "SO00001"
    assert result["customer"] == "ABC Industries"
    assert result["items"] == 1


def test_fraction_confidence_normalized_to_percent():
    pipeline = _pipeline(FakeOdoo())
    result = pipeline.process("telegram", "text", _parsed(confidence=0.98))
    assert result["status"] == "success"
    assert result["confidence"] == 98.0


def test_pending_confirmation_then_confirm():
    pipeline = _pipeline(FakeOdoo())
    result = pipeline.process("telegram", "text", _parsed(confidence=85))
    assert result["status"] == "pending"
    confirmed = pipeline.confirm_order(result["order_id"], actor="telegram")
    assert confirmed["status"] == "success"
    assert confirmed["sales_order"] == "SO00001"


def test_low_confidence_goes_to_review():
    pipeline = _pipeline(FakeOdoo())
    result = pipeline.process("telegram", "text", _parsed(confidence=60))
    assert result["status"] == "review"
    assert pipeline.confirm_order(result["order_id"])["status"] == "error"


def test_image_always_awaits_confirmation():
    pipeline = _pipeline(FakeOdoo())
    result = pipeline.process("telegram", "image", _parsed(confidence=97))
    assert result["status"] == "pending"
    assert len(result["items_detail"]) == 1
    assert result["items_detail"][0]["product_name"] == "Keyboard"
    confirmed = pipeline.confirm_order(result["order_id"], actor="telegram")
    assert confirmed["status"] == "success"
    assert confirmed["sales_order"] == "SO00001"


def test_auto_create_all_orders_skips_confirmation_for_image():
    pipeline = _pipeline(FakeOdoo(), auto_create_all_orders=True)
    result = pipeline.process("telegram", "image", _parsed(confidence=85))
    assert result["status"] == "success"
    assert result["sales_order"] == "SO00001"
    assert result["mode"] == "auto"


def test_auto_create_all_orders_still_reviews_invalid():
    pipeline = _pipeline(FakeOdoo(), auto_create_all_orders=True)
    result = pipeline.process("telegram", "text", _parsed(confidence=99, product="Flying Car"))
    assert result["status"] == "review"


def test_pending_result_includes_items_detail():
    pipeline = _pipeline(FakeOdoo())
    result = pipeline.process("telegram", "image", _parsed(confidence=90))
    assert result["status"] == "pending"
    assert len(result["items_detail"]) == 1
    assert result["items_detail"][0]["product_name"] == "Keyboard"


def test_invalid_product_goes_to_review_even_with_high_confidence():
    pipeline = _pipeline(FakeOdoo())
    result = pipeline.process("telegram", "text", _parsed(confidence=99, product="Flying Car"))
    assert result["status"] == "review"


def test_existing_customer_uses_partner_id():
    pipeline = _pipeline(TrackingOdoo())
    result = pipeline.process("email", "text", _parsed(confidence=97, customer="Existing Co"))
    assert result["status"] == "success"
    assert pipeline.odoo.last_partner_id == 42


class TrackingOdoo(FakeOdoo):
    def __init__(self):
        self.last_partner_id = None

    def create_sale_order(self, partner_id, items, notes=""):
        self.last_partner_id = partner_id
        return super().create_sale_order(partner_id, items, notes)


def test_unknown_customer_gets_created():
    pipeline = _pipeline(TrackingOdoo())
    result = pipeline.process("email", "text", _parsed(confidence=97))
    assert result["status"] == "success"
    assert pipeline.odoo.last_partner_id == 99