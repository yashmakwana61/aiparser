import json
from datetime import datetime, timezone

from order_parser.config import Settings
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.duplicate_detector import DuplicateDetector, fingerprint_order
from order_parser.resolution.models import ResolutionStatus
from order_parser.resolution.order_resolver import OrderResolver


CATALOG = [
    {"id": 123, "name": "BREAD WHITE 400 GMS", "default_code": "BW400", "list_price": 500.0, "uom_id": 1, "taxes_id": [5]},
    {"id": 125, "name": "Keyboard", "default_code": "KB01", "list_price": 50.0, "uom_id": 2, "taxes_id": [5]},
]
PARTNERS = {
    42: {"id": 42, "name": "Existing Co", "email": "buyer@existing.com", "phone": "+15551234567"},
}
UOMS = {
    1: {"id": 1, "name": "Units", "factor": 1.0},
    2: {"id": 2, "name": "Box", "factor": 1.0},
}


class FakeResolutionOdoo:
    enabled = True

    def __init__(self):
        self.partner_calls = []

    def fetch_product_catalog(self):
        return [dict(p) for p in CATALOG]

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
        results = []
        for uom in UOMS.values():
            ok = True
            for field, op, value in domain:
                if op == "=ilike" and str(uom.get(field, "")).casefold() != str(value).casefold():
                    ok = False
            if ok:
                results.append(uom)
        return results[:limit]

    def find_uom(self, name):
        return None

    def get_uom(self, uom_id):
        return UOMS.get(int(uom_id))

    def get_partner_pricelist(self, partner_id):
        return 8 if partner_id == 42 else None

    def compute_pricelist_price(self, pricelist_id, product_id, quantity, partner_id=None):
        if pricelist_id == 8:
            return {123: 480.0, 125: 45.0}.get(product_id)
        return None

    def get_partner_fiscal_position(self, partner_id):
        return None

    def map_taxes_through_fiscal_position(self, fiscal_position_id, tax_ids):
        return list(tax_ids)

    def default_sale_taxes(self):
        return []

    def create_partner(self, customer):
        raise AssertionError("resolution layer must never create partners")


class EmptyPendingStore:
    def list(self, status=None):
        return []


def _resolver(tmp_path, pending_store=None, settings_overrides=None):
    overrides = {}
    overrides.update(settings_overrides or {})
    settings = Settings(**overrides)
    odoo = FakeResolutionOdoo()
    aliases = AliasStore(tmp_path / "aliases")
    conversions_path = tmp_path / "conversions.json"
    conversions_path.write_text(
        json.dumps({"conversions": [{"match": ["dozen"], "to_uom_name": "Units", "factor": 12.0, "approved_by": "ops"}]}),
        encoding="utf-8",
    )
    resolver = OrderResolver(
        odoo,
        catalog=CatalogProvider(odoo),
        alias_store=aliases,
        pending_store=pending_store or EmptyPendingStore(),
        settings=settings,
        conversions_path=conversions_path,
    )
    aliases.create_product("white bread 400", 123, created_by="staff")
    return resolver


def _parsed(items, customer="Existing Co", confidence=96.0, ai_items=None, ai_customer=None):
    order = OrderModel(
        customer=CustomerModel(name=customer),
        items=[ItemModel(**item) for item in items],
        metadata=MetadataModel(confidence=confidence),
    )
    ai_response = {"items": ai_items or [], "customer": ai_customer or {}}
    return ParsedOrder(order=order, ai_response=ai_response)


def test_full_successful_resolution(tmp_path):
    resolver = _resolver(tmp_path)
    parsed = _parsed(
        [
            {"product_name": "Keyboard", "quantity": 2},
            {"product_name": "White Bread 400", "quantity": 2, "unit_price": 500.0, "uom": "DOZEN"},
        ]
    )
    resolved = resolver.resolve(parsed)

    assert resolved.blocking_issues == []
    assert resolved.is_auto_eligible is True

    customer = resolved.customer
    assert customer.status == ResolutionStatus.RESOLVED
    assert customer.resolution_method == "exact_name"
    assert customer.partner_id == 42

    keyboard = resolved.items[0]
    assert keyboard.product.product_id == 125
    assert keyboard.product.resolution_method == "exact_name"
    assert keyboard.uom.resolution_method == "product_sales_uom"
    assert keyboard.uom.uom_name == "Box"
    assert keyboard.price.resolution_method == "customer_pricelist"
    assert keyboard.price.value == 45.0
    assert keyboard.price.pricelist_id == 8
    assert keyboard.tax.resolution_method == "product_tax"
    assert keyboard.tax.tax_ids == [5]

    bread = resolved.items[1]
    assert bread.product.product_id == 123
    assert bread.product.resolution_method == "global_alias"
    assert bread.product.confidence == 98.0
    assert bread.product.product_name == "BREAD WHITE 400 GMS"
    assert bread.uom.resolution_method == "approved_conversion"
    assert bread.quantity_effective == 24.0
    assert bread.price.resolution_method == "explicit_order_price"
    assert bread.price.source == "order"

    summary = resolved.summary()
    assert summary["blocking"] == []
    assert summary["auto_eligible"] is True
    assert summary["items"][1]["quantity_effective"] == 24.0

    dumped = bread.price.model_dump()
    for key in ("value", "source", "resolution_method"):
        assert key in dumped
    assert dumped["value"] == 500.0
    assert dumped["source"] == "order"


def test_unresolvable_fields_aggregate_blocking_issues(tmp_path):
    resolver = _resolver(tmp_path)
    parsed = _parsed([{"product_name": "Flying Car", "quantity": 0}], customer="Mystery Buyer Ltd")
    resolved = resolver.resolve(parsed)

    codes = {issue.code for issue in resolved.blocking_issues}
    assert "PRODUCT_UNRESOLVED" in codes
    assert "CUSTOMER_UNRESOLVED" in codes
    assert "QUANTITY_CONFLICT" in codes
    # Phase 18: UOM, price, tax are non-blocking warnings.
    assert "UOM_UNRESOLVED" not in codes
    assert "PRICE_MISSING" not in codes
    assert "TAX_UNRESOLVED" not in codes
    warning_codes = {w.code for w in resolved.warnings}
    assert "UOM_MISSING" in warning_codes
    assert "PRICE_MISSING" in warning_codes
    assert "TAX_MISSING" in warning_codes
    assert resolved.is_auto_eligible is False


def test_duplicate_recent_order_is_blocked(tmp_path):
    parsed = _parsed([{"product_name": "Keyboard", "quantity": 2}])

    class SeededStore(EmptyPendingStore):
        def __init__(self, fingerprint):
            self.fingerprint = fingerprint

        def list(self, status=None):
            return [
                {
                    "order_id": "prior001",
                    "status": "pending",
                    "fingerprint": self.fingerprint,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]

    first_resolver = _resolver(tmp_path)
    seeded = SeededStore(fingerprint_order(parsed.order))
    second_resolver = _resolver(tmp_path / "second", pending_store=seeded)

    clean = first_resolver.resolve(parsed)
    assert clean.blocking_issues == []

    duplicated = second_resolver.resolve(parsed)
    assert any(issue.code == "DUPLICATE_ORDER" for issue in duplicated.blocking_issues)
    assert duplicated.is_auto_eligible is False


def test_customer_scoped_alias_used_in_full_flow(tmp_path):
    resolver = _resolver(tmp_path)
    resolver.alias_store.create_product("kb pro gamer", 125, customer_id=42)
    parsed = _parsed([{"product_name": "Kb Pro Gamer", "quantity": 1}])
    resolved = resolver.resolve(parsed)
    assert resolved.items[0].product.resolution_method == "customer_alias"
    assert resolved.items[0].product.confidence == 97.0
    assert resolved.is_auto_eligible is True
