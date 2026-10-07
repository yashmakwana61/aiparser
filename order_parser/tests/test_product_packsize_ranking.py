"""Pack-size-aware product ranking: distinctive tokens beat partial overlaps."""

from order_parser.config import Settings
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.models import ResolutionStatus
from order_parser.resolution.product_resolver import (
    ProductResolver,
    product_fuzzy_score,
    product_tokens,
    score_product_pair,
)

CATALOG = [
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
    {"id": 203, "name": 'BURGER BROWN BREAD 4.5" PLAIN', "default_code": "BB45",
     "list_price": 20.0, "uom_id": 1, "taxes_id": []},
    {"id": 228, "name": 'BURGER WHITE BREAD 3" PLAIN', "default_code": "BW3",
     "list_price": 18.0, "uom_id": 1, "taxes_id": []},
    {"id": 699, "name": 'HOT DOG WHITE BREAD 6" PLAIN', "default_code": "HD6",
     "list_price": 22.0, "uom_id": 1, "taxes_id": []},
    {"id": 165, "name": "BREAD WHITE 700 GMS", "default_code": "BW700",
     "list_price": 36.5, "uom_id": 1, "taxes_id": [32]},
]


class FakeOdoo:
    enabled = True

    def fetch_product_catalog(self):
        return [dict(p) for p in CATALOG]


def _resolver(tmp_path):
    return ProductResolver(CatalogProvider(FakeOdoo()), AliasStore(tmp_path / "aliases"),
                           Settings())


def test_pack_size_tokens_split():
    assert product_tokens("Kulcha Plain 6pcs") == "kulcha plain 6 pc"
    assert product_tokens("Bread White 700g") == "bread white 700 gm"
    # Shared normalization is untouched: alias keys stay valid.
    from order_parser.resolution.normalization import normalize_name
    assert normalize_name("Bread White 700g") == "bread white 700g"


def test_token_set_separates_true_match_from_partial_overlap():
    ts_true, _ = score_product_pair("Kulcha Plain 6pcs", "KULCHA BREAD (6 Pcs)")
    ts_burger, _ = score_product_pair("Kulcha Plain 6pcs", 'HOT DOG WHITE BREAD 6" PLAIN')
    assert ts_true > ts_burger
    assert ts_true >= 72.0


def test_kulcha_ranks_first(tmp_path):
    resolved = _resolver(tmp_path).resolve("Kulcha Plain 6pcs")
    assert resolved.status == ResolutionStatus.RESOLVED
    assert resolved.product_id == 753
    assert resolved.product_name == "KULCHA BREAD (6 Pcs)"


def test_typo_tolerance_preserved_via_legacy_path(tmp_path):
    resolved = _resolver(tmp_path).resolve("Burgre Brown Bead")
    assert resolved.status == ResolutionStatus.RESOLVED
    assert resolved.product_id == 203


def test_unrelated_stays_unresolved(tmp_path):
    resolved = _resolver(tmp_path).resolve("Unobtainium Widget Xyz")
    assert resolved.status == ResolutionStatus.UNRESOLVED
    assert resolved.product_id is None


def test_exact_and_alias_paths_untouched(tmp_path):
    resolver = _resolver(tmp_path)
    exact = resolver.resolve("BREAD WHITE 700 GMS")
    assert exact.resolution_method == "exact_name" and exact.product_id == 165


def test_rare_token_veto_blocks_wrong_winner(tmp_path):
    from order_parser.resolution.models import ResolutionStatus
    from order_parser.resolution.product_resolver import (
        idf_weights, product_tokens)

    resolver = _resolver(tmp_path)
    products = list(CATALOG) + [
        {"id": 1568, "name": "PAV 250GM PKT", "default_code": "PAV250",
         "list_price": 40.0, "uom_id": 1, "taxes_id": []},
    ]
    weights = idf_weights([product_tokens(p["name"]).split() for p in products])
    pav_product = next(p for p in products if p["id"] == 1568)
    out = resolver._rare_token_veto(
        "Kulcha 250gm", product_tokens("Kulcha 250gm").split(),
        pav_product, products, weights)
    assert out is not None
    assert out.status == ResolutionStatus.AMBIGUOUS
    assert any(c.get("product_id") == 753 for c in out.candidates)


def test_rare_token_veto_passes_covered_winner(tmp_path):
    from order_parser.resolution.models import ResolutionStatus
    from order_parser.resolution.product_resolver import (
        idf_weights, product_tokens)

    resolver = _resolver(tmp_path)
    products = list(CATALOG)
    weights = idf_weights([product_tokens(p["name"]).split() for p in products])
    kulcha = next(p for p in products if p["id"] == 753)
    out = resolver._rare_token_veto(
        "Kulcha Plain 6pcs", product_tokens("Kulcha Plain 6pcs").split(),
        kulcha, products, weights)
    assert out is None
