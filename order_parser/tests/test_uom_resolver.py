import json

from order_parser.config import Settings
from order_parser.resolution.models import ProductResolution, ResolutionStatus
from order_parser.resolution.uom_resolver import UOMResolver


class FakeUomOdoo:
    enabled = True

    def __init__(self, uoms=None):
        self._uoms = uoms or {}

    def search_uoms(self, domain, limit=1):
        results = []
        for uom in self._uoms.values():
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
        return self._uoms.get(int(uom_id))


UOMS = {
    1: {"id": 1, "name": "Units", "factor": 1.0},
    2: {"id": 2, "name": "Box", "factor": 1.0},
}


def _resolver(odoo=None, conversions=None, tmp_path=None):
    path = None
    if conversions is not None and tmp_path is not None:
        path = tmp_path / "uom_conversions.json"
        path.write_text(json.dumps({"conversions": conversions}), encoding="utf-8")
    settings = Settings()
    return UOMResolver(odoo or FakeUomOdoo(UOMS), conversions_path=path, settings=settings)


def _product(uom_id=2):
    return ProductResolution(details={"uom_id": uom_id})


def test_explicit_uom_resolves_from_odoo():
    resolution, factor = _resolver().resolve("Box", _product())
    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.resolution_method == "explicit_uom"
    assert resolution.uom_id == 2
    assert factor == 1.0


def test_missing_uom_falls_back_to_product_sales_uom():
    resolution, factor = _resolver().resolve("", _product(uom_id=1))
    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.resolution_method == "product_sales_uom"
    assert resolution.uom_id == 1
    assert factor == 1.0


def test_default_units_word_uses_product_uom_not_catalog_guess():
    resolution, _ = _resolver().resolve("units", _product(uom_id=2))
    assert resolution.resolution_method == "product_sales_uom"
    assert resolution.uom_name == "Box"


def test_approved_conversion_multiplies_quantity(tmp_path):
    conversions = [
        {"match": ["dozen", "dz"], "to_uom_name": "Units", "factor": 12.0, "approved_by": "ops"},
    ]
    resolver = _resolver(conversions=conversions, tmp_path=tmp_path)
    resolution, factor = resolver.resolve("DOZEN", _product())
    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.resolution_method == "approved_conversion"
    assert resolution.uom_name == "Units"
    assert factor == 12.0


def test_unresolvable_uom_is_exception(tmp_path):
    resolver = _resolver(odoo=FakeUomOdoo({}), conversions=[], tmp_path=tmp_path)
    resolution, factor = resolver.resolve("parsecs", ProductResolution(details={}))
    assert resolution.status == ResolutionStatus.UNRESOLVED
    assert resolution.reason == "uom_unresolved"
    assert factor == 1.0


def test_seed_conversions_file_loads():
    resolver = UOMResolver(FakeUomOdoo(UOMS))
    conversions = resolver._load_conversions()
    assert conversions, "seeded data/uom_conversions.json should load"


def test_product_sales_uom_accepts_odoo_many2one_tuple():
    """Odoo read() returns uom_id as [id, 'Name']; the resolver must not choke.

    Regression: _get_uom(int([1, 'Units'])) raised TypeError, was swallowed,
    and every order line became UOM_UNRESOLVED despite valid product data.
    """

    requested = []

    class RecordingUomOdoo(FakeUomOdoo):
        def get_uom(self, uom_id):
            requested.append(uom_id)
            return super().get_uom(uom_id)

    resolver = _resolver(odoo=RecordingUomOdoo(UOMS))
    product = ProductResolution(
        raw_name="Bread Brown Jumbo 1.8kg",
        status=ResolutionStatus.RESOLVED,
        source="odoo",
        resolution_method=None,
        value="Bread",
        confidence=90.0,
        reference_id=6,
        product_id=6,
        product_name="Bread",
        details={"uom_id": [1, "Units"]},
    )

    resolution, factor = resolver.resolve(None, product)

    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.resolution_method is not None
    assert resolution.uom_name == "Units"
    assert requested == [1]  # numeric id passed through, not the raw list
