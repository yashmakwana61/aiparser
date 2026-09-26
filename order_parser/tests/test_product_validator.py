from order_parser.models import ItemModel
from order_parser.validators.product_validator import ProductValidator


class FakeOdoo:
    enabled = True

    def fetch_product_catalog(self):
        return [
            {"id": 1, "name": "Dell Latitude 5400"},
            {"id": 2, "name": "HP Laptop"},
            {"id": 3, "name": "Keyboard"},
            {"id": 4, "name": "Laptop"},
        ]


def test_fuzzy_matches_typos():
    validator = ProductValidator(FakeOdoo())
    items = [
        ItemModel(product_name="Dell Lattitude 5400", quantity=1),
        ItemModel(product_name="Keybord", quantity=2),
        ItemModel(product_name="Lappy", quantity=1),
    ]
    results = validator.validate(items)
    assert all(r["valid"] for r in results)
    assert results[0]["product_id"] == 1
    assert results[0]["matched_name"] == "Dell Latitude 5400"
    assert results[1]["product_id"] == 3


def test_quantity_zero_is_invalid():
    validator = ProductValidator(FakeOdoo())
    results = validator.validate([ItemModel(product_name="Keyboard", quantity=0)])
    assert results[0]["valid"] is False
    assert results[0]["reason"] == "quantity_must_be_positive"


def test_unknown_product_is_invalid():
    validator = ProductValidator(FakeOdoo())
    results = validator.validate([ItemModel(product_name="Flying Car", quantity=1)])
    assert results[0]["valid"] is False
    assert results[0]["reason"] == "product_not_found"


class CreatingOdoo(FakeOdoo):
    def __init__(self):
        self.created = []

    def create_product(self, name, price=None):
        self.created.append((name, price))
        return 100 + len(self.created)


def test_unknown_product_is_auto_created():
    odoo = CreatingOdoo()
    validator = ProductValidator(odoo, auto_create=True)
    results = validator.validate([ItemModel(product_name="Flying Car", quantity=1, unit_price=25.5)])
    assert results[0]["valid"] is True
    assert results[0]["auto_created"] is True
    assert results[0]["product_id"] == 101
    assert odoo.created == [("Flying Car", 25.5)]


def test_auto_create_uses_zero_price_when_missing():
    odoo = CreatingOdoo()
    validator = ProductValidator(odoo, auto_create=True)
    results = validator.validate([ItemModel(product_name="Gadget", quantity=1)])
    assert results[0]["valid"] is True
    assert odoo.created == [("Gadget", 0.0)]


def test_odoo_unavailable():
    class OfflineOdoo(FakeOdoo):
        enabled = False

    validator = ProductValidator(OfflineOdoo())
    results = validator.validate([ItemModel(product_name="Keyboard", quantity=1)])
    assert results[0]["valid"] is False
    assert results[0]["reason"] == "odoo_unavailable"