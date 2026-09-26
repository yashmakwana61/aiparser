from order_parser.config import Settings
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.order_resolver import OrderResolver
from order_parser.services.pipeline import OrderPipeline


CATALOG = [
    {"id": 123, "name": "BREAD WHITE 400 GMS", "default_code": "BW400", "list_price": 500.0, "uom_id": 1, "taxes_id": [5]},
    {"id": 125, "name": "Keyboard", "default_code": "KB01", "list_price": 50.0, "uom_id": 2, "taxes_id": [5]},
]
PARTNERS = {
    42: {"id": 42, "name": "Existing Co", "email": "", "phone": ""},
}


class FullFakeOdoo:
    enabled = True

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
        return []

    def find_uom(self, name):
        return None

    def get_uom(self, uom_id):
        return {2: {"id": 2, "name": "Box", "factor": 1.0}}.get(int(uom_id))

    def get_partner_pricelist(self, partner_id):
        return None

    def compute_pricelist_price(self, *args):
        return None

    def get_partner_fiscal_position(self, partner_id):
        return None

    def map_taxes_through_fiscal_position(self, fp, tax_ids):
        return list(tax_ids)

    def default_sale_taxes(self):
        return [5]

    def create_partner(self, customer):
        raise AssertionError("resolver-mode pipeline must never create partners")

    def create_sale_order(self, partner_id, items, notes=""):
        assert all(item.product_id for item in items)
        self.last_partner_id = partner_id
        self.last_items = list(items)
        return {"id": 1, "name": "SO00001"}


class LegacyFakeOdoo(FullFakeOdoo):
    """Pre-resolution behaviour: fuzzy validator + auto partner creation."""

    def __init__(self):
        self.last_partner_id = None

    def find_partner(self, customer):
        if customer.name == "Existing Co":
            return {"id": 42, "name": "Existing Co"}
        return None

    def create_partner(self, customer):
        return 99

    def create_sale_order(self, partner_id, items, notes=""):
        self.last_partner_id = partner_id
        return {"id": 1, "name": "SO00001"}

    def default_sale_taxes(self):
        raise AssertionError("legacy pipeline should not touch resolver reads")


def _settings(**overrides):
    defaults = {"auto_create_products": False, "auto_create_all_orders": False}
    defaults.update(overrides)
    return Settings(**defaults)


def _pipeline(tmp_path, settings=None, odoo=None):
    odoo = odoo or FullFakeOdoo()
    aliases = AliasStore(tmp_path / "aliases")
    resolver = OrderResolver(
        odoo,
        catalog=CatalogProvider(odoo),
        alias_store=aliases,
        pending_store=_NullStore(),
        settings=settings or _settings(),
    )
    pipeline = OrderPipeline(odoo, settings=settings or _settings(), resolver=resolver)
    pipeline.pending_store.directory = tmp_path / "pending"
    from order_parser.core.pending_store import PendingStore

    pipeline.pending_store = PendingStore(tmp_path / "pending")
    resolver.pending_store = pipeline.pending_store
    resolver.duplicates.pending_store = pipeline.pending_store
    return pipeline


class _NullStore:
    def list(self, status=None):
        return []


def _parsed(confidence, product="Keyboard", qty=2, customer="Existing Co", ai_items=None):
    order = OrderModel(
        customer=CustomerModel(name=customer),
        items=[ItemModel(product_name=product, quantity=qty)],
        metadata=MetadataModel(confidence=confidence),
    )
    return ParsedOrder(order=order, ai_response={"items": ai_items or [], "customer": {"name": customer}})


def test_clean_deterministic_order_auto_creates(tmp_path):
    pipeline = _pipeline(tmp_path)
    result = pipeline.process("telegram", "text", _parsed(confidence=96))
    assert result["status"] == "success"
    assert result["sales_order"] == "SO00001"
    assert result["mode"] == "auto"
    assert result["resolution_blocked"] == []
    assert result["resolution_warnings"] == []
    assert pipeline.odoo.last_partner_id == 42


def test_fuzzy_only_match_caps_into_pending_band_even_at_high_ai_confidence(tmp_path):
    pipeline = _pipeline(tmp_path)
    result = pipeline.process("telegram", "text", _parsed(confidence=99, product="Keybord"))
    assert result["status"] == "pending"
    confirmed = pipeline.confirm_order(result["order_id"], actor="telegram")
    assert confirmed["status"] == "success"


def test_ambiguous_product_forces_review_regardless_of_confidence(tmp_path):
    from order_parser.core.pending_store import PendingStore

    ambiguous_odoo = FullFakeOdoo()
    ambiguous_odoo.fetch_product_catalog = lambda: CATALOG + [
        {"id": 126, "name": "Keyboard", "default_code": "KB02", "list_price": 51.0, "uom_id": 2, "taxes_id": []},
    ]
    resolver = OrderResolver(
        ambiguous_odoo,
        catalog=CatalogProvider(ambiguous_odoo),
        alias_store=AliasStore(tmp_path / "amb-aliases"),
        pending_store=_NullStore(),
        settings=_settings(),
    )
    ambiguous_pipeline = OrderPipeline(ambiguous_odoo, settings=_settings(), resolver=resolver)
    ambiguous_pipeline.pending_store = PendingStore(tmp_path / "amb-pending")
    resolver.pending_store = ambiguous_pipeline.pending_store
    resolver.duplicates.pending_store = ambiguous_pipeline.pending_store

    result = ambiguous_pipeline.process("telegram", "text", _parsed(confidence=99))
    assert result["status"] == "review"
    assert "PRODUCT_AMBIGUOUS" in result["resolution_blocked"]
    assert ambiguous_pipeline.confirm_order(result["order_id"])["status"] == "error"


def test_unknown_customer_routes_to_review_never_created(tmp_path):
    pipeline = _pipeline(tmp_path)
    result = pipeline.process("email", "text", _parsed(confidence=97, customer="Mystery Buyer Ltd"))
    assert result["status"] == "review"
    assert "CUSTOMER_UNRESOLVED" in result["resolution_blocked"]


def test_duplicate_recent_pending_order_is_reviewed(tmp_path):
    pipeline = _pipeline(tmp_path)
    first = pipeline.process("telegram", "text", _parsed(confidence=85))
    assert first["status"] == "pending"
    second = pipeline.process("telegram", "text", _parsed(confidence=85))
    assert second["status"] == "review"
    assert "DUPLICATE_ORDER" in second["resolution_blocked"]


def test_auto_create_all_orders_cannot_bypass_blocking_issues(tmp_path):
    ambiguous_odoo = FullFakeOdoo()
    ambiguous_odoo.fetch_product_catalog = lambda: CATALOG + [
        {"id": 126, "name": "Keyboard", "default_code": "KB02", "list_price": 51.0, "uom_id": 2, "taxes_id": []},
    ]
    resolver = OrderResolver(
        ambiguous_odoo,
        catalog=CatalogProvider(ambiguous_odoo),
        alias_store=AliasStore(tmp_path / "override-aliases"),
        pending_store=_NullStore(),
        settings=_settings(auto_create_all_orders=True),
    )
    pipeline = OrderPipeline(
        ambiguous_odoo,
        settings=_settings(auto_create_all_orders=True),
        resolver=resolver,
    )
    from order_parser.core.pending_store import PendingStore

    pipeline.pending_store = PendingStore(tmp_path / "override-pending")
    resolver.pending_store = pipeline.pending_store
    result = pipeline.process("telegram", "image", _parsed(confidence=85))
    assert result["status"] == "review"


def test_legacy_pipeline_without_resolver_is_unchanged(tmp_path):
    odoo = LegacyFakeOdoo()
    pipeline = OrderPipeline(odoo, settings=_settings())
    from order_parser.core.pending_store import PendingStore

    pipeline.pending_store = PendingStore(tmp_path / "legacy-pending")
    result = pipeline.process("email", "text", _parsed(confidence=97, customer="Unknown Buyer"))
    assert result["status"] == "success"
    assert result["sales_order"] == "SO00001"
    assert odoo.last_partner_id == 99
