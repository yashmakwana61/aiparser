from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.models import ResolutionStatus
from order_parser.resolution.product_resolver import ProductResolver


class FakeCatalog:
    def __init__(self, products):
        self._products = products

    def products(self):
        return list(self._products)

    def by_sku(self):
        from order_parser.resolution.normalization import normalize_sku

        index = {}
        for product in self._products:
            sku = normalize_sku(product.get("default_code"))
            if sku:
                index.setdefault(sku, []).append(product)
        return index

    def by_normalized_name(self):
        from order_parser.resolution.normalization import normalized_variants

        index = {}
        for product in self._products:
            for variant in normalized_variants(product.get("name")):
                index.setdefault(variant, []).append(product)
        return index

    def get(self, product_id):
        return next((p for p in self._products if p["id"] == product_id), None)


class FailingCatalog(FakeCatalog):
    def by_sku(self):
        raise RuntimeError("odoo down")


CATALOG = [
    {"id": 123, "name": "BREAD WHITE 400 GMS", "default_code": "BW400", "list_price": 500.0, "uom_id": 1, "taxes_id": [5]},
    {"id": 124, "name": "BREAD BROWN 400 GMS", "default_code": "BB400", "list_price": 480.0, "uom_id": 1, "taxes_id": [5]},
    {"id": 125, "name": "Keyboard", "default_code": "KB01", "list_price": 50.0, "uom_id": 2, "taxes_id": [5]},
]


def _resolver(products=CATALOG, alias_store=None):
    return ProductResolver(FakeCatalog(products), alias_store)


def test_exact_product_match():
    result = _resolver().resolve("Keyboard")
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "exact_name"
    assert result.confidence == 100.0
    assert result.product_id == 125
    assert result.product_name == "Keyboard"


def test_sku_match():
    result = _resolver().resolve("bw-400")
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "sku_exact"
    assert result.product_id == 123


def test_normalized_product_match():
    result = _resolver().resolve("keyboard,")
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "normalized_name"
    assert result.confidence == 99.0
    assert result.product_id == 125


def test_token_sorted_normalized_match():
    result = _resolver().resolve("GMS 400 WHITE BREAD")
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "normalized_name"
    assert result.product_id == 123


def test_alias_match(tmp_path):
    store = AliasStore(tmp_path)
    store.create_product("white bread 400", 123, created_by="staff")
    resolver = _resolver(alias_store=store)
    result = resolver.resolve("White Bread 400")
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "global_alias"
    assert result.confidence == 98.0
    assert result.product_id == 123
    record = store.list_aliases("product")[0]
    assert record.usage_count == 1


def test_customer_specific_alias_wins_over_global(tmp_path):
    store = AliasStore(tmp_path)
    store.create_product("wb", 124)
    store.create_product("wb", 123, customer_id=42)
    resolver = _resolver(alias_store=store)
    scoped = resolver.resolve("WB", partner_id=42)
    assert scoped.resolution_method == "customer_alias"
    assert scoped.product_id == 123
    assert scoped.confidence == 97.0
    fallback = resolver.resolve("WB")
    assert fallback.resolution_method == "global_alias"
    assert fallback.product_id == 124


def test_fuzzy_match_is_capped_below_auto_band():
    result = _resolver().resolve("Keybord")
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "fuzzy_match"
    assert result.product_id == 125
    assert result.confidence <= 89.0
    assert result.details["default_code"] == "KB01"


def test_duplicate_names_are_ambiguous():
    duplicated = CATALOG + [
        {"id": 126, "name": "Keyboard", "default_code": "KB02", "list_price": 55.0, "uom_id": 2, "taxes_id": []},
    ]
    result = _resolver(duplicated).resolve("Keyboard")
    assert result.status == ResolutionStatus.AMBIGUOUS
    assert result.reason == "multiple_products_share_this_name"
    assert len(result.candidates) == 2
    assert {c["product_id"] for c in result.candidates} == {125, 126}


def test_unknown_product_unresolved():
    result = _resolver().resolve("Flying Car")
    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason == "no_candidate_above_cutoff"


def test_catalog_outage_never_guesses():
    resolver = ProductResolver(FailingCatalog(CATALOG))
    result = resolver.resolve("Keyboard")
    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason == "catalog_unavailable"


def test_missing_name_unresolved():
    result = _resolver().resolve("")
    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason == "missing_product_name"
