from order_parser.normalizers.order_normalizer import OrderNormalizer


def test_normalize_basic():
    raw = {
        "customer": {"name": "ABC Industries", "email": "abc@example.com", "phone": "123"},
        "items": [{"product_name": "Keyboard", "quantity": 2}],
        "notes": "Urgent",
        "confidence": 95,
    }
    order = OrderNormalizer.normalize(raw, "telegram", "text")
    assert order.customer.name == "ABC Industries"
    assert order.customer.email == "abc@example.com"
    assert order.items[0].product_name == "Keyboard"
    assert order.items[0].quantity == 2
    assert order.items[0].uom == "Units"
    assert order.metadata.confidence == 95.0
    assert order.metadata.notes == "Urgent"


def test_normalize_skips_empty_items():
    raw = {
        "items": [
            {"product_name": "   ", "quantity": 1},
            {"product_name": "Mouse", "quantity": "3 pcs"},
        ]
    }
    order = OrderNormalizer.normalize(raw, "email", "text")
    assert len(order.items) == 1
    assert order.items[0].product_name == "Mouse"
    assert order.items[0].quantity == 3.0


def test_normalize_string_customer():
    order = OrderNormalizer.normalize({"customer": "ACME", "items": []}, "tg", "text")
    assert order.customer.name == "ACME"


def test_normalize_clamps_confidence():
    assert OrderNormalizer.normalize({"confidence": 150}, "tg", "text").metadata.confidence == 100.0
    assert OrderNormalizer.normalize({"confidence": -5}, "tg", "text").metadata.confidence == 0.0