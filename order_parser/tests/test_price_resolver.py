from order_parser.config import Settings
from order_parser.models import ItemModel
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.models import ProductResolution, ResolutionStatus
from order_parser.resolution.price_resolver import PriceResolver


class FakePriceOdoo:
    enabled = True

    def __init__(self, pricelists=None):
        # partner_id -> (pricelist_id, {product_id: unit_price})
        self._pricelists = pricelists or {}

    def get_partner_pricelist(self, partner_id):
        entry = self._pricelists.get(partner_id)
        return entry[0] if entry else None

    def compute_pricelist_price(self, pricelist_id, product_id, quantity, partner_id=None):
        for pl_id, prices in self._pricelists.values():
            if pl_id == pricelist_id and product_id in prices:
                return prices[product_id]
        return None


CATALOG_PRODUCT = {"id": 123, "name": "BREAD WHITE 400 GMS", "default_code": "BW400", "list_price": 500.0}


class FakeCatalog:
    def __init__(self, products):
        self._products = products

    def get(self, product_id):
        return next((p for p in self._products if p["id"] == product_id), None)


def _resolver(pricelists=None, settings_overrides=None):
    overrides = {"approved_price_fallback": False}
    overrides.update(settings_overrides or {})
    settings = Settings(**overrides)
    odoo = FakePriceOdoo(pricelists)
    return PriceResolver(odoo, catalog=FakeCatalog([CATALOG_PRODUCT]), settings=settings)


def _item(price=None, qty=2):
    return ItemModel(product_name="Bread", quantity=qty, unit_price=price)


def _product():
    return ProductResolution(status=ResolutionStatus.RESOLVED, product_id=123)


def test_explicit_price_wins_and_is_tagged_as_order_source():
    result = _resolver(pricelists={42: (7, {123: 450.0})}).resolve(_item(500.0), _product(), 42)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "explicit_order_price"
    assert result.source == "order"
    assert result.value == 500.0
    assert result.details["odoo_reference_price"] == 450.0


def test_customer_pricelist_used_when_no_explicit_price():
    result = _resolver(pricelists={42: (8, {123: 450.0})}).resolve(_item(), _product(), 42)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "customer_pricelist"
    assert result.pricelist_id == 8
    assert result.value == 450.0


def test_product_sales_price_is_next_fallback():
    result = _resolver().resolve(_item(), _product(), None)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "product_sales_price"
    assert result.value == 500.0


def test_missing_price_blocks_when_mandatory():
    empty_catalog = _resolver()
    empty_catalog.catalog = FakeCatalog([{"id": 999, "name": "Ghost", "list_price": None}])
    product = ProductResolution(status=ResolutionStatus.RESOLVED, product_id=999)
    result = empty_catalog.resolve(_item(), product, None)
    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason == "price_missing"


def test_approved_zero_price_fallback_requires_opt_in():
    resolver = _resolver(settings_overrides={"approved_price_fallback": True})
    resolver.catalog = FakeCatalog([{"id": 999, "name": "Ghost", "list_price": None}])
    product = ProductResolution(status=ResolutionStatus.RESOLVED, product_id=999)
    result = resolver.resolve(_item(), product, None)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "approved_fallback"
    assert result.value == 0.0


def test_large_deviation_from_odoo_reference_is_flagged():
    result = _resolver(pricelists={42: (7, {123: 400.0})}).resolve(_item(600.0), _product(), 42)
    assert result.resolution_method == "explicit_order_price"
    deviation = result.details.get("deviation_pct")
    assert deviation is not None and deviation > 25.0


def test_small_deviation_not_flagged():
    result = _resolver(pricelists={42: (7, {123: 480.0})}).resolve(_item(500.0), _product(), 42)
    assert "deviation_pct" not in result.details
