"""Golden replay harness: the parser's training loop.

Real production names/ids (no credentials). Every matching change must keep
or improve this suite: add a case for each new failure mode observed in
production, then make it pass. Resolve-rate over these cases is the
release gate for matching changes.

Extend by appending to CUSTOMER_CASES / PRODUCT_CASES with the production
outcome attached.
"""

from order_parser.config import Settings
from order_parser.models import CustomerModel
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.customer_resolver import CustomerResolver
from order_parser.resolution.models import ResolutionStatus
from order_parser.resolution.normalization import normalize_name, normalized_variants
from order_parser.resolution.order_resolver import OrderResolver
from order_parser.resolution.product_resolver import ProductResolver

# ---------------------------------------------------------------- snapshot


PARTNERS = {
    1: {"id": 1, "name": "HOT CAKES PRIVATE LIMITED", "city": "New Delhi",
        "zip": "110020", "street": "C 40 Okhla", "vat": "07AAECH4859D1ZK"},
    785: {"id": 785, "name": "HOT CAKES PRIVATE LIMITED-GGN", "city": "Gurgaon",
          "zip": "122001", "street": "Sohna Road", "vat": "06AAECH4859D1Z1"},
    726: {"id": 726, "name": "EIH LIMITED", "city": "New Delhi",
          "zip": "110001", "street": "Oberoi House", "vat": ""},
    1126: {"id": 1126, "name": "ITC HOTELS LIMITED. - ITC MAURYA", "city": "New Delhi",
           "zip": "110021", "street": "Sardar Patel Marg", "vat": "07AAAH1234A1Z1"},
    1127: {"id": 1127, "name": "ITC HOTELS LIMITED.-TAURU", "city": "Tauru",
           "zip": "122105", "street": "Grand Bharat", "vat": "06AAAH1234A1Z2"},
    2690: {"id": 2690, "name": "ITC Hotels Limited \u2013 Sheraton Saket",
           "city": "New Delhi", "zip": "110017", "street": "District Centre, Saket",
           "vat": "07AAHCI2404A1Z9"},
    1717: {"id": 1717, "name": "ONLY COFFEE NOTHING ELSE", "city": "",
           "zip": "", "street": "", "vat": ""},
    1908: {"id": 1908, "name": "RADISSON NOIDA", "city": "Noida",
           "zip": "201301", "street": "Stadium Road", "vat": ""},
    1905: {"id": 1905, "name": "RADISSON BLU KAUSHAMBI (KAD)", "city": "Ghaziabad",
           "zip": "201010", "street": "Kaushambi", "vat": ""},
}

PRODUCTS = [
    {"id": 663, "name": "Golden Grain Kulcha Bread", "default_code": "GGKB",
     "list_price": 10.0, "uom_id": 1, "taxes_id": []},
    {"id": 753, "name": "KULCHA BREAD (6 Pcs)", "default_code": "KB6",
     "list_price": 27.0, "uom_id": 1, "taxes_id": [32]},
    {"id": 754, "name": "KULCHA BREAD 200 GMS (4Pcs)", "default_code": "KB200",
     "list_price": 30.0, "uom_id": 1, "taxes_id": []},
    {"id": 755, "name": "KULCHA BREAD MINI", "default_code": "KBM",
     "list_price": 12.0, "uom_id": 1, "taxes_id": []},
    {"id": 756, "name": "KULCHA BREAD WHITE 200GM", "default_code": "KBW",
     "list_price": 15.0, "uom_id": 1, "taxes_id": []},
    {"id": 1568, "name": "PAV 250GM PKT", "default_code": "PAV250",
     "list_price": 40.0, "uom_id": 1, "taxes_id": []},
    {"id": 203, "name": 'BURGER BROWN BREAD 4.5" PLAIN', "default_code": "BB45",
     "list_price": 20.0, "uom_id": 1, "taxes_id": []},
    {"id": 165, "name": "BREAD WHITE 700 GMS", "default_code": "BW700",
     "list_price": 36.5, "uom_id": 1, "taxes_id": [32]},
    {"id": 176, "name": "BREADS", "default_code": "BRD",
     "list_price": 48.5, "uom_id": 1, "taxes_id": [32]},
    {"id": 111, "name": "BIRTHDAY CANDLE(6PC/PKT)", "default_code": "BC6",
     "list_price": 5.0, "uom_id": 1, "taxes_id": []},
]


class GoldenOdoo:
    """Production-shaped fake: real names, real matching semantics."""

    enabled = True

    def __init__(self, partners=None, products=None):
        self._partners = partners if partners is not None else PARTNERS
        self._products = products if products is not None else PRODUCTS

    # -- partners --
    def search_partners(self, domain, limit=2, fields=None):
        results = []
        for partner in self._partners.values():
            ok = True
            for field, op, value in domain:
                actual = partner.get(field)
                if op == "=ilike" and str(actual or "").casefold() != str(value).casefold():
                    ok = False
                elif op == "ilike" and str(value).casefold() not in str(actual or "").casefold():
                    ok = False
                elif op == "in" and actual not in (value or []):
                    ok = False
            if ok:
                row = {"id": partner["id"], "name": partner["name"]}
                if fields:
                    row = {k: partner.get(k) for k in fields if k in partner}
                    row.setdefault("id", partner["id"])
                    row.setdefault("name", partner["name"])
                results.append(row)
        return results[:limit]

    def get_partner(self, partner_id):
        partner = self._partners.get(int(partner_id))
        return dict(partner) if partner else None

    # -- products (CatalogProvider-compatible surface) --
    def fetch_product_catalog(self):
        return [dict(p) for p in self._products]

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

    def find_uom(self, name):
        return None


KULCHA_FAMILY = {663, 753, 754, 755, 756}


def _customer_resolver(odoo=None):
    return CustomerResolver(odoo or GoldenOdoo(), aliases=None, settings=Settings())


def _product_resolver(tmp_path, odoo=None):
    odoo = odoo or GoldenOdoo()
    return ProductResolver(CatalogProvider(odoo), AliasStore(tmp_path / "aliases"),
                           Settings())


# ---------------------------------------------------------------- customers


def test_golden_customer_case_insensitive_comma():
    resolved = _customer_resolver().resolve(CustomerModel(name="only coffee, nothing else"))
    assert (resolved.status, resolved.partner_id) == (ResolutionStatus.RESOLVED, 1717)


def test_golden_customer_itc_saket_unit():
    customer = CustomerModel(name="ITC Hotels Limited", address="District Centre, Saket",
                             city="New Delhi", zip_code="110017", gstin="07AAHCI2404A1Z9")
    resolved = _customer_resolver().resolve(customer)
    assert resolved.status == ResolutionStatus.RESOLVED
    assert resolved.partner_id == 2690


def test_golden_customer_bare_itc_asks():
    resolved = _customer_resolver().resolve(CustomerModel(name="ITC Hotels Limited"))
    assert resolved.status == ResolutionStatus.AMBIGUOUS
    assert resolved.partner_id is None


def test_golden_customer_radisson_never_noida():
    resolved = _customer_resolver().resolve(
        CustomerModel(name="Radisson Blu Plaza Delhi Airport"))
    assert resolved.partner_id != 1908
    assert resolved.status == ResolutionStatus.AMBIGUOUS


def test_golden_collector_refused(tmp_path):
    from order_parser.models import ItemModel, MetadataModel, OrderModel, ParsedOrder

    settings = Settings(never_customer_names="HOT CAKES PRIVATE LIMITED")
    odoo = GoldenOdoo()
    resolver = OrderResolver(odoo, catalog=CatalogProvider(odoo),
                             alias_store=AliasStore(tmp_path / "aliases"),
                             pending_store=None, settings=settings)
    order = OrderModel(customer=CustomerModel(name="HOT CAKES PRIVATE LTD"),
                       items=[ItemModel(product_name="Bread", quantity=1)],
                       metadata=MetadataModel())
    resolved = resolver.resolve(ParsedOrder(order=order, ai_response={"customer": {}, "items": []}))
    assert resolved.customer.partner_id is None
    codes = [issue.code for issue in resolved.blocking_issues]
    assert "COLLECTOR_AS_CUSTOMER" in codes


# ---------------------------------------------------------------- products


def test_golden_product_kulcha_plain(tmp_path):
    resolved = _product_resolver(tmp_path).resolve("Kulcha Plain 6pcs")
    assert resolved.product_id == 753


def test_golden_product_pav_exact(tmp_path):
    resolved = _product_resolver(tmp_path).resolve("PAV 250GM PKT")
    assert (resolved.status, resolved.product_id) == (ResolutionStatus.RESOLVED, 1568)


def test_golden_product_kulcha_pack_variant_stays_in_family(tmp_path):
    resolved = _product_resolver(tmp_path).resolve("Kulcha 250gm (pkt/6 pc)")
    if resolved.status == ResolutionStatus.RESOLVED:
        assert resolved.product_id in KULCHA_FAMILY
    else:
        # Genuinely uncertain pack variant: must stay askable, and the true
        # family must be among the offered candidates (never PAV alone).
        assert resolved.status == ResolutionStatus.AMBIGUOUS
        ids = {c.get("product_id") for c in resolved.candidates or []}
        assert ids & KULCHA_FAMILY, "kulcha options must be offered"
        assert resolved.product_id is None


def test_golden_product_alias_survives(tmp_path):
    resolver = _product_resolver(tmp_path)
    resolver.aliases.create_product("Bread White 700g", 165, created_by="golden")
    resolved = resolver.resolve("Bread White 700g")
    assert resolved.product_id == 165


def test_golden_product_typo_tolerance(tmp_path):
    resolved = _product_resolver(tmp_path).resolve("Burgre Brown Bead")
    assert resolved.status == ResolutionStatus.RESOLVED
    assert "BURGER" in (resolved.product_name or "")
