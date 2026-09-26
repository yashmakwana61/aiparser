"""Phase 18: Permissive resolution tests.

Validates that the parser produces READY_FOR_ODOO payloads when only UOM,
price or tax are missing, and that terminal errors (MISSING_CUSTOMER,
MISSING_ORDER_DETAILS, etc.) are correctly returned.
"""

import json
from datetime import datetime, timezone

from order_parser.config import Settings
from order_parser.models import (
    CustomerModel,
    ItemModel,
    MetadataModel,
    OrderModel,
    ParsedOrder,
)
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.duplicate_detector import fingerprint_order
from order_parser.resolution.models import (
    PRICE_MISSING,
    PRICE_MISSING_WARNING,
    TAX_MISSING_WARNING,
    UOM_MISSING_WARNING,
    ResolutionStatus,
)
from order_parser.resolution.order_resolver import OrderResolver
from order_parser.services.pipeline import OrderPipeline


# ── Fakes ────────────────────────────────────────────────────────────

CATALOG = [
    {
        "id": 123,
        "name": "BREAD WHITE 400 GMS",
        "default_code": "BW400",
        "list_price": 500.0,
        "uom_id": 1,
        "taxes_id": [5],
    },
    {
        "id": 125,
        "name": "Keyboard",
        "default_code": "KB01",
        "list_price": 50.0,
        "uom_id": 2,
        "taxes_id": [5],
    },
]
PARTNERS = {
    42: {"id": 42, "name": "Existing Co", "email": "", "phone": ""},
}
UOMS = {
    1: {"id": 1, "name": "Units", "factor": 1.0},
    2: {"id": 2, "name": "Box", "factor": 1.0},
}


class PermissiveFakeOdoo:
    """Odoo stub: resolves customer + product but deliberately does NOT
    resolve price for some products, does NOT resolve tax, and does NOT
    resolve UOM — to test permissive behaviour."""

    enabled = True
    include_list_price = True

    def fetch_product_catalog(self):
        catalog = [dict(p) for p in CATALOG]
        if not self.include_list_price:
            for p in catalog:
                p["list_price"] = None
        return catalog

    def get_partner(self, partner_id):
        return PARTNERS.get(int(partner_id))

    def search_partners(self, domain, limit=2):
        results = []
        for partner in PARTNERS.values():
            ok = True
            for field, op, value in domain:
                actual = str(partner.get(field) or "")
                if op == "=ilike" and actual.casefold() != str(value).casefold():
                    ok = False
                elif op == "ilike" and str(value).casefold() not in actual.casefold():
                    ok = False
            if ok:
                results.append({"id": partner["id"], "name": partner["name"]})
        return results[:limit]

    def search_uoms(self, domain, limit=1):
        return []

    def find_uom(self, name):
        return None

    def get_uom(self, uom_id):
        return UOMS.get(int(uom_id))

    def get_partner_pricelist(self, partner_id):
        return None

    def compute_pricelist_price(self, *args):
        return None

    def get_partner_fiscal_position(self, partner_id):
        return None

    def map_taxes_through_fiscal_position(self, fp, tax_ids):
        return list(tax_ids)

    def default_sale_taxes(self):
        return []

    def create_partner(self, customer):
        raise AssertionError("resolver must never create partners")

    def create_sale_order(self, partner_id, items, notes=""):
        self.last_partner_id = partner_id
        self.last_items = list(items)
        return {"id": 1, "name": "SO00001"}


class NoUomFakeOdoo(PermissiveFakeOdoo):
    """Products have no uom_id — UOM cannot resolve."""

    def fetch_product_catalog(self):
        catalog = [dict(p) for p in CATALOG]
        for p in catalog:
            p.pop("uom_id", None)
            if not self.include_list_price:
                p["list_price"] = None
        return catalog


class NoTaxFakeOdoo(PermissiveFakeOdoo):
    """Products have no taxes_id and company has no defaults."""

    def fetch_product_catalog(self):
        catalog = [dict(p) for p in CATALOG]
        for p in catalog:
            p["taxes_id"] = []
        return catalog

    def default_sale_taxes(self):
        return []


class NoProductFakeOdoo(PermissiveFakeOdoo):
    """Returns an empty catalog — no products can be resolved."""

    def fetch_product_catalog(self):
        return []


class NoCustomerFakeOdoo(PermissiveFakeOdoo):
    """No partner matches."""

    def search_partners(self, domain, limit=2):
        return []


class FailingOdoo(PermissiveFakeOdoo):
    """Odoo is unavailable."""

    enabled = False


class _NullStore:
    def list(self, status=None):
        return []
    def get(self, order_id):
        return None
    def save(self, record):
        pass
    def delete(self, order_id):
        pass


# ── Helpers ──────────────────────────────────────────────────────────

def _settings(**overrides):
    defaults = {
        "auto_create_products": False,
        "auto_create_all_orders": False,
        "strict_resolution": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _resolver(tmp_path, odoo=None, settings_overrides=None):
    odoo = odoo or PermissiveFakeOdoo()
    settings = Settings(**{
        "auto_create_products": False,
        "auto_create_all_orders": False,
        "strict_resolution": False,
        **(settings_overrides or {}),
    })
    aliases = AliasStore(tmp_path / "aliases")
    conversions_path = tmp_path / "conversions.json"
    conversions_path.write_text(
        json.dumps({"conversions": []}),
        encoding="utf-8",
    )
    return OrderResolver(
        odoo,
        catalog=CatalogProvider(odoo),
        alias_store=aliases,
        pending_store=_NullStore(),
        settings=settings,
        conversions_path=conversions_path,
    )


def _pipeline(tmp_path, odoo=None, settings_overrides=None):
    odoo = odoo or PermissiveFakeOdoo()
    settings = Settings(**{
        "auto_create_products": False,
        "auto_create_all_orders": False,
        "strict_resolution": False,
        **(settings_overrides or {}),
    })
    aliases = AliasStore(tmp_path / "aliases")
    conversions_path = tmp_path / "conversions.json"
    conversions_path.write_text(
        json.dumps({"conversions": []}),
        encoding="utf-8",
    )
    resolver = OrderResolver(
        odoo,
        catalog=CatalogProvider(odoo),
        alias_store=aliases,
        pending_store=_NullStore(),
        settings=settings,
        conversions_path=conversions_path,
    )
    pipeline = OrderPipeline(odoo, settings=settings, resolver=resolver)
    from order_parser.core.pending_store import PendingStore

    pipeline.pending_store = PendingStore(tmp_path / "pending")
    resolver.pending_store = pipeline.pending_store
    resolver.duplicates.pending_store = pipeline.pending_store
    return pipeline


def _parsed(confidence=96.0, product="Keyboard", qty=2, customer="Existing Co",
            uom=None, price=None):
    order = OrderModel(
        customer=CustomerModel(name=customer),
        items=[ItemModel(product_name=product, quantity=qty, uom=uom, unit_price=price)],
        metadata=MetadataModel(confidence=confidence),
    )
    return ParsedOrder(order=order, ai_response={"items": [], "customer": {"name": customer}})


# ── Tests ────────────────────────────────────────────────────────────


# 1. Complete order
def test_complete_order_ready_for_odoo(tmp_path):
    pipeline = _pipeline(tmp_path)
    result = pipeline.process("telegram", "text", _parsed(confidence=96))
    assert result["status"] == "success"
    assert result["readiness_status"] == "READY_FOR_ODOO"
    assert result["blocking_for_odoo"] is False
    assert result["missing_information"] == []


# 2. Missing price — still READY_FOR_ODOO
def test_missing_price_still_ready(tmp_path):
    odoo = PermissiveFakeOdoo()
    odoo.include_list_price = False
    pipeline = _pipeline(tmp_path, odoo=odoo)
    result = pipeline.process("telegram", "text", _parsed(confidence=96, price=None))
    assert result["readiness_status"] == "READY_FOR_ODOO"
    assert "PRICE_MISSING" not in result.get("resolution_blocked", [])
    assert "PRICE_MISSING" in result.get("warnings", [])
    item = result["items_detail_readiness"][0]
    assert item["price"] is None
    assert "price" in item["missing_fields"]
    assert result["blocking_for_odoo"] is False


# 3. Missing tax — still READY_FOR_ODOO
def test_missing_tax_still_ready(tmp_path):
    pipeline = _pipeline(tmp_path, odoo=NoTaxFakeOdoo())
    result = pipeline.process("telegram", "text", _parsed(confidence=96))
    assert result["readiness_status"] == "READY_FOR_ODOO"
    assert "TAX_MISSING" in result.get("warnings", [])
    item = result["items_detail_readiness"][0]
    assert item["tax_ids"] == []
    assert "tax" in item["missing_fields"]
    assert result["blocking_for_odoo"] is False


# 4. Missing UOM — still READY_FOR_ODOO
def test_missing_uom_still_ready(tmp_path):
    pipeline = _pipeline(tmp_path, odoo=NoUomFakeOdoo())
    result = pipeline.process("telegram", "text", _parsed(confidence=96, uom=None))
    assert result["readiness_status"] == "READY_FOR_ODOO"
    assert "UOM_MISSING" in result.get("warnings", [])
    item = result["items_detail_readiness"][0]
    assert item["uom"] is None
    assert "uom" in item["missing_fields"]
    assert result["blocking_for_odoo"] is False


# 5. Missing customer → MISSING_CUSTOMER
def test_missing_customer_terminal(tmp_path):
    pipeline = _pipeline(tmp_path, odoo=NoCustomerFakeOdoo())
    result = pipeline.process("email", "text", _parsed(confidence=97, customer="Mystery Buyer"))
    assert result["readiness_status"] == "MISSING_CUSTOMER"
    assert result["blocking_for_odoo"] is True
    assert result["status"] == "review"


# 6. Missing product → review (PRODUCT_UNKNOWN)
def test_missing_product_terminal(tmp_path):
    pipeline = _pipeline(tmp_path, odoo=NoProductFakeOdoo())
    result = pipeline.process("telegram", "text", _parsed(confidence=96, product="Flying Car"))
    assert result["readiness_status"] == "PRODUCT_UNKNOWN"
    assert result["blocking_for_odoo"] is True
    assert result["status"] == "review"


# 7. Missing quantity → INVALID_QUANTITY
def test_missing_quantity_terminal(tmp_path):
    pipeline = _pipeline(tmp_path)
    result = pipeline.process("telegram", "text", _parsed(confidence=96, qty=0))
    assert result["readiness_status"] == "INVALID_QUANTITY"
    assert result["blocking_for_odoo"] is True
    assert result["status"] == "review"


# 8. Ambiguous product → review
def test_ambiguous_product_review(tmp_path):
    ambiguous_odoo = PermissiveFakeOdoo()
    ambiguous_odoo.fetch_product_catalog = lambda: CATALOG + [
        {"id": 126, "name": "Keyboard", "default_code": "KB02", "list_price": 51.0, "uom_id": 2, "taxes_id": []},
    ]
    pipeline = _pipeline(tmp_path, odoo=ambiguous_odoo)
    result = pipeline.process("telegram", "text", _parsed(confidence=99))
    assert result["readiness_status"] == "PRODUCT_AMBIGUOUS"
    assert result["blocking_for_odoo"] is True
    assert result["status"] == "review"
    assert "PRODUCT_AMBIGUOUS" in result["resolution_blocked"]


# 9. Duplicate request → review
def test_duplicate_request_blocked(tmp_path):
    pipeline = _pipeline(tmp_path)
    first = pipeline.process("telegram", "text", _parsed(confidence=85))
    assert first["status"] == "pending"
    second = pipeline.process("telegram", "text", _parsed(confidence=85))
    assert second["status"] == "review"
    assert "DUPLICATE_ORDER" in second["resolution_blocked"]


# 10. Image order with missing price → pending (not review)
def test_image_order_with_missing_price(tmp_path):
    odoo = PermissiveFakeOdoo()
    odoo.include_list_price = False
    pipeline = _pipeline(tmp_path, odoo=odoo)
    result = pipeline.process("telegram", "image", _parsed(confidence=96, price=None))
    # Image orders go to pending even with high confidence
    assert result["status"] == "pending"
    assert result["readiness_status"] == "READY_FOR_ODOO"
    assert "PRICE_MISSING" in result.get("warnings", [])


# 11. PDF order regression
def test_pdf_order_regression(tmp_path):
    pipeline = _pipeline(tmp_path)
    parsed = _parsed(confidence=96)
    parsed.order.metadata.input_type = "pdf"
    result = pipeline.process("email", "pdf", parsed)
    assert result["readiness_status"] == "READY_FOR_ODOO"


# 12. Excel order regression
def test_excel_order_regression(tmp_path):
    pipeline = _pipeline(tmp_path)
    parsed = _parsed(confidence=96)
    parsed.order.metadata.input_type = "excel"
    result = pipeline.process("email", "excel", parsed)
    assert result["readiness_status"] == "READY_FOR_ODOO"


# 13. Uneven text order
def test_uneven_text_order(tmp_path):
    pipeline = _pipeline(tmp_path)
    order = OrderModel(
        customer=CustomerModel(name="Existing Co"),
        items=[
            ItemModel(product_name="Keyboard", quantity=5),
            ItemModel(product_name="Flying Car", quantity=1),
        ],
        metadata=MetadataModel(confidence=88),
    )
    parsed = ParsedOrder(order=order, ai_response={"items": [], "customer": {"name": "Existing Co"}})
    result = pipeline.process("telegram", "text", parsed)
    # Mixed: one product resolved, one unresolved → still goes to review
    assert result["status"] == "review"
    assert "PRODUCT_UNRESOLVED" in result["resolution_blocked"]


# 14. Odoo failure
def test_odoo_unavailable(tmp_path):
    pipeline = _pipeline(tmp_path, odoo=FailingOdoo())
    result = pipeline.process("telegram", "text", _parsed(confidence=96))
    assert result["readiness_status"] == "ODOO_UNAVAILABLE"
    assert result["blocking_for_odoo"] is True


# 15. Strict resolution mode restores legacy blocking
def test_strict_resolution_mode_blocks(tmp_path):
    """In strict mode, missing UOM/price/tax revert to blocking issues."""
    # Use a product with no uom_id, no list_price, and no taxes
    # — and no company defaults.
    strict_odoo = PermissiveFakeOdoo()
    strict_odoo.include_list_price = False
    pipeline = _pipeline(
        tmp_path,
        odoo=strict_odoo,
        settings_overrides={"strict_resolution": True},
    )
    # First remove uom_id from catalog so UOM can't resolve
    original_fetch = strict_odoo.fetch_product_catalog
    def no_uom_catalog():
        cat = original_fetch()
        for p in cat:
            p.pop("uom_id", None)
            p["taxes_id"] = []
        return cat
    strict_odoo.fetch_product_catalog = no_uom_catalog

    result = pipeline.process("telegram", "text", _parsed(confidence=96))
    assert result["status"] == "review"
    blocked = result["resolution_blocked"]
    # In strict mode, these are blocking
    assert "UOM_UNRESOLVED" in blocked or "PRICE_MISSING" in blocked or "TAX_UNRESOLVED" in blocked
    # In permissive mode, they'd be warnings only
    warnings = result.get("resolution_warnings", [])
    assert "UOM_UNRESOLVED" not in warnings
    assert "TAX_UNRESOLVED" not in warnings


# 16. Missing information aggregated across items
def test_missing_information_aggregated(tmp_path):
    odoo = NoUomFakeOdoo()
    odoo.include_list_price = False
    pipeline = _pipeline(tmp_path, odoo=odoo)
    order = OrderModel(
        customer=CustomerModel(name="Existing Co"),
        items=[
            ItemModel(product_name="Keyboard", quantity=2, uom=None, unit_price=None),
            ItemModel(product_name="Keyboard", quantity=3, uom="Box", unit_price=10.0),
        ],
        metadata=MetadataModel(confidence=96),
    )
    parsed = ParsedOrder(order=order, ai_response={"items": [], "customer": {"name": "Existing Co"}})
    result = pipeline.process("telegram", "text", parsed)
    assert result["readiness_status"] == "READY_FOR_ODOO"
    # First item: missing uom + price; second item: has explicit price but missing uom
    missing = set(result["missing_information"])
    assert "uom" in missing
    assert "price" in missing
    assert result["blocking_for_odoo"] is False


# 17. blocking_for_tally reflects missing financial data
def test_blocking_for_tally_indication(tmp_path):
    odoo = NoUomFakeOdoo()
    odoo.include_list_price = False
    pipeline = _pipeline(tmp_path, odoo=odoo)
    result = pipeline.process("telegram", "text", _parsed(confidence=96))
    # NoUomFakeOdoo: no UOM resolution + no list_price → UOM and price missing
    assert result["blocking_for_tally"] is True
    assert result["blocking_for_odoo"] is False


# 18. Readiness response structure validation
def test_readiness_response_structure(tmp_path):
    pipeline = _pipeline(tmp_path)
    result = pipeline.process("telegram", "text", _parsed(confidence=96))
    # Verify all Phase 18 fields are present
    assert "readiness_status" in result
    assert "customer_detail" in result
    assert "items_detail_readiness" in result
    assert "missing_information" in result
    assert "warnings" in result
    assert "blocking_for_odoo" in result
    assert "blocking_for_tally" in result
    # customer_detail structure
    cust = result["customer_detail"]
    assert "raw_name" in cust
    assert "resolved" in cust
    assert "partner_id" in cust
    assert "partner_name" in cust
    # item_detail structure
    item = result["items_detail_readiness"][0]
    assert "raw_name" in item
    assert "product_id" in item
    assert "product_name" in item
    assert "quantity" in item
    assert "uom" in item
    assert "price" in item
    assert "tax_ids" in item
    assert "missing_fields" in item


# 19. Telegram format_result includes missing information
def test_telegram_format_includes_missing_info(tmp_path):
    from order_parser.channels.telegram_handler import format_result

    odoo = PermissiveFakeOdoo()
    odoo.include_list_price = False
    pipeline = _pipeline(tmp_path, odoo=odoo)
    result = pipeline.process("telegram", "text", _parsed(confidence=96))
    text = format_result(result)
    assert "Missing" in text


# 20. Email channel produces same readiness contract
def test_email_channel_readiness_contract(tmp_path):
    pipeline = _pipeline(tmp_path)
    result = pipeline.process("email", "text", _parsed(confidence=96))
    assert result["readiness_status"] == "READY_FOR_ODOO"
    assert "customer_detail" in result
    assert "items_detail_readiness" in result
    assert isinstance(result["missing_information"], list)
    assert isinstance(result["warnings"], list)


# 21. Auto-create succeeds with missing price in permissive mode
def test_auto_create_with_missing_price(tmp_path):
    odoo = PermissiveFakeOdoo()
    odoo.include_list_price = False
    pipeline = _pipeline(
        tmp_path,
        odoo=odoo,
        settings_overrides={"auto_create_all_orders": True, "auto_create_products": False},
    )
    result = pipeline.process("telegram", "text", _parsed(confidence=96, price=None))
    assert result["status"] == "success"
    assert result["sales_order"] == "SO00001"
    assert result["readiness_status"] == "READY_FOR_ODOO"
