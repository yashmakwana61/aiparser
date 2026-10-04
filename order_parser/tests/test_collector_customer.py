"""Collector-as-customer guard: vendors/order-collectors must never resolve
as the customer (e.g. HOT CAKES PRIVATE LIMITED on ITC purchase orders)."""

from xmlrpc.client import Fault

from order_parser.ai.prompts import TEXT_PROMPT, VISION_PROMPT
from order_parser.config import Settings
from order_parser.integrations.odoo_client import OdooClient
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.normalizers.order_normalizer import OrderNormalizer
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.models import COLLECTOR_AS_CUSTOMER, ResolutionStatus
from order_parser.resolution.normalization import matches_never_customer
from order_parser.resolution.order_resolver import OrderResolver


PARTNERS = {
    1: {"id": 1, "name": "HOT CAKES PRIVATE LIMITED"},
    42: {"id": 42, "name": "Existing Co"},
    99: {"id": 99, "name": "ITC Sheraton Saket"},
}


class FakeOdoo:
    enabled = True

    def __init__(self):
        self.searched_names = []

    def fetch_product_catalog(self):
        return []

    def get_partner(self, partner_id):
        return PARTNERS.get(int(partner_id))

    def search_partners(self, domain, limit=2):
        for field, op, value in domain:
            if field == "name":
                self.searched_names.append(str(value))
        results = []
        for partner in PARTNERS.values():
            if str(value).casefold() in partner["name"].casefold():
                results.append({"id": partner["id"], "name": partner["name"]})
        return results[:limit]

    def search_uoms(self, domain, limit=1):
        return []

    def find_uom(self, name):
        return None

    def get_partner_pricelist(self, partner_id):
        return None

    def compute_pricelist_price(self, pricelist_id, product_id, quantity, partner_id=None):
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


def _resolver(tmp_path, **overrides):
    settings = Settings(never_customer_names="HOT CAKES PRIVATE LIMITED", **overrides)
    odoo = FakeOdoo()
    resolver = OrderResolver(
        odoo,
        catalog=CatalogProvider(odoo),
        alias_store=AliasStore(tmp_path / "aliases"),
        pending_store=EmptyPendingStore(),
        settings=settings,
    )
    resolver._fake = odoo
    return resolver


def _parsed(customer, deliver_to=None, sender=None):
    order = OrderModel(
        customer=CustomerModel(name=customer),
        items=[ItemModel(product_name="Bread", quantity=1)],
        metadata=MetadataModel(confidence=99.0),
        sender_name=sender,
        deliver_to=CustomerModel(name=deliver_to) if deliver_to else None,
    )
    return ParsedOrder(order=order, ai_response={"customer": {"name": customer}, "items": []})


def test_collector_reroutes_to_deliver_to(tmp_path):
    resolver = _resolver(tmp_path)
    resolved = resolver.resolve(_parsed("HOT CAKES PRIVATE LTD", deliver_to="ITC Sheraton Saket"))
    assert resolved.customer.status == ResolutionStatus.RESOLVED
    assert resolved.customer.partner_id == 99
    assert resolved.customer.details.get("customer_name_effective") == "ITC Sheraton Saket"
    assert resolved.customer.details.get("collector_rerouted_from") == "HOT CAKES PRIVATE LTD"
    assert "HOT CAKES" not in " ".join(resolver._fake.searched_names).upper()
    assert all(bi.code != COLLECTOR_AS_CUSTOMER for bi in resolved.blocking_issues)


def test_collector_variant_matches_entry(tmp_path):
    resolver = _resolver(tmp_path)
    resolved = resolver.resolve(_parsed("HOT CAKES PRIVATE LIMITED-GGN"))
    assert resolved.customer.status == ResolutionStatus.UNRESOLVED
    assert resolved.customer.reason == "collector_as_customer"
    assert any(bi.code == COLLECTOR_AS_CUSTOMER for bi in resolved.blocking_issues)


def test_collector_without_deliver_to_blocked_explicitly(tmp_path):
    resolver = _resolver(tmp_path)
    resolved = resolver.resolve(_parsed("HOT CAKES PRIVATE LTD"))
    codes = [bi.code for bi in resolved.blocking_issues]
    assert COLLECTOR_AS_CUSTOMER in codes
    assert "CUSTOMER_AMBIGUOUS" not in codes
    assert resolved.customer.partner_id is None


def test_human_override_wins_over_collector_guard(tmp_path):
    resolver = _resolver(tmp_path)
    resolved = resolver.resolve(_parsed("HOT CAKES PRIVATE LTD"), staff_partner_id=42)
    assert resolved.customer.status == ResolutionStatus.RESOLVED
    assert resolved.customer.partner_id == 42


def test_explicit_collector_partner_refused(tmp_path):
    resolver = _resolver(tmp_path)
    resolved = resolver.resolve(_parsed("Someone"), explicit_customer_id=1)
    assert resolved.customer.status == ResolutionStatus.UNRESOLVED
    assert resolved.customer.reason == "collector_as_customer"
    assert any(bi.code == "COLLECTOR_AS_CUSTOMER" for bi in resolved.blocking_issues)
    assert resolved.customer.partner_id is None


def test_staff_selected_collector_partner_refused(tmp_path):
    resolver = _resolver(tmp_path)
    resolved = resolver.resolve(_parsed("Someone"), staff_partner_id=1)
    assert resolved.customer.status == ResolutionStatus.UNRESOLVED
    assert any(bi.code == "COLLECTOR_AS_CUSTOMER" for bi in resolved.blocking_issues)


def test_exact_collector_name_match_refused(tmp_path):
    resolver = _resolver(tmp_path)
    resolved = resolver.resolve(_parsed("HOT CAKES PRIVATE LIMITED"))
    assert resolved.customer.status == ResolutionStatus.UNRESOLVED
    assert resolved.customer.reason == "collector_as_customer"


def test_ordinary_customer_unaffected(tmp_path):
    resolver = _resolver(tmp_path)
    resolved = resolver.resolve(_parsed("Existing Co"))
    assert resolved.customer.status == ResolutionStatus.RESOLVED
    assert resolved.customer.partner_id == 42


def test_normalizer_passes_sender_and_deliver_to():
    order = OrderNormalizer.normalize(
        {
            "customer": {"name": "HOT CAKES PRIVATE LTD"},
            "sender": {"name": "HOT CAKES PRIVATE LTD"},
            "deliver_to": {"name": "ITC Hotels Limited, Sheraton New Delhi", "city": "New Delhi"},
            "items": [],
        },
        "telegram",
        "pdf",
    )
    assert order.sender_name == "HOT CAKES PRIVATE LTD"
    assert order.deliver_to is not None
    assert order.deliver_to.name == "ITC Hotels Limited, Sheraton New Delhi"
    assert order.deliver_to.city == "New Delhi"


def test_normalizer_sender_string_and_missing_parties():
    order = OrderNormalizer.normalize({"customer": {"name": "X"}, "sender": "Vendor Co", "items": []}, "api", "text")
    assert order.sender_name == "Vendor Co"
    assert order.deliver_to is None


def test_prompts_declare_buyer_vs_vendor_rule():
    for prompt in (TEXT_PROMPT, VISION_PROMPT):
        assert "sender" in prompt
        assert "deliver_to" in prompt
        assert "NEVER the customer" in prompt


def test_matches_never_customer_abbreviation_tolerant():
    entries = "HOT CAKES PRIVATE LIMITED"
    assert matches_never_customer("HOT CAKES PRIVATE LTD", entries) == "HOT CAKES PRIVATE LIMITED"
    assert matches_never_customer("HOT CAKES PRIVATE LIMITED-GGN", entries) == "HOT CAKES PRIVATE LIMITED"
    assert matches_never_customer("hot cakes pvt ltd", entries) == "HOT CAKES PRIVATE LIMITED"
    assert matches_never_customer("ITC Hotels Limited", entries) is None
    assert matches_never_customer("", entries) is None
    assert matches_never_customer("HOT CAKES PRIVATE LTD", "") is None


def test_fiscal_position_falls_back_to_new_field_name(monkeypatch):
    client = OdooClient(url="http://odoo.test", db="db1", username="u", password="p")
    calls = []

    def fake_execute(model, method, args, kwargs=None):
        calls.append(args[1][0])
        if args[1][0] == "property_account_position_id":
            return [{"property_account_position_id": [7, "X"]}]
        raise Fault(1, "Invalid field")

    monkeypatch.setattr(client, "execute_kw", fake_execute)
    assert client.get_partner_fiscal_position(5) == 7
    assert calls[0] == "property_account_position_id"


def test_fiscal_position_returns_none_when_both_fields_missing(monkeypatch):
    client = OdooClient(url="http://odoo.test", db="db1", username="u", password="p")

    def fake_execute(model, method, args, kwargs=None):
        raise Fault(1, "Invalid field")

    monkeypatch.setattr(client, "execute_kw", fake_execute)
    assert client.get_partner_fiscal_position(5) is None


def test_default_sale_taxes_quiet_fallback(monkeypatch):
    client = OdooClient(url="http://odoo.test", db="db1", username="u", password="p")

    def fake_execute(model, method, args, kwargs=None):
        raise Fault(1, "method 'ir.default.get' does not exist")

    monkeypatch.setattr(client, "execute_kw", fake_execute)
    assert client.default_sale_taxes() == []
