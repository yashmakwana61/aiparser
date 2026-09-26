from order_parser.resolution.alias_store import AliasConflictError, AliasStore
from order_parser.resolution.duplicate_detector import DuplicateDetector, fingerprint_order
from order_parser.models import CustomerModel, ItemModel, OrderModel


def test_product_alias_roundtrip_and_usage(tmp_path):
    store = AliasStore(tmp_path)
    record = store.create_product("White Bread 400", 123, created_by="staff")
    assert record.normalized_alias == "white bread 400"
    found = store.find_product("white bread 400")
    assert found is not None and found.target_id == 123

    store.record_usage("product", record.id)
    reloaded = AliasStore(tmp_path)
    persisted = reloaded.find_product("white bread 400")
    assert persisted.usage_count == 1


def test_customer_scoped_lookup_precedence(tmp_path):
    store = AliasStore(tmp_path)
    store.create_product("wb", 124)
    store.create_product("wb", 123, customer_id=42)
    assert store.find_product("wb", customer_id=42).target_id == 123
    assert store.find_product("wb").target_id == 124
    assert store.find_product("wb", customer_id=99).target_id == 124


def test_conflicting_target_raises(tmp_path):
    store = AliasStore(tmp_path)
    store.create_product("wb", 123)
    try:
        store.create_product("WB!", 999)
        raise AssertionError("expected AliasConflictError")
    except AliasConflictError:
        pass


def test_same_target_create_is_idempotent(tmp_path):
    store = AliasStore(tmp_path)
    first = store.create_product("wb", 123)
    second = store.create_product("wb", 123)
    assert first.id == second.id


def test_deactivate_hides_alias(tmp_path):
    store = AliasStore(tmp_path)
    record = store.create_product("wb", 123)
    store.deactivate("product", record.id)
    assert store.find_product("wb") is None


def test_list_query_filter(tmp_path):
    store = AliasStore(tmp_path)
    store.create_product("white bread", 123)
    store.create_product("brown bread", 124)
    hits = store.list_aliases("product", query="bread")
    assert len(hits) == 2
    hits = store.list_aliases("product", query="brown")
    assert len(hits) == 1 and hits[0].target_id == 124


class FakeStore:
    def __init__(self, records):
        self._records = records

    def list(self, status=None):
        return list(self._records)


def _order(name="Existing Co", product="Keyboard", qty=2, price=None):
    return OrderModel(
        customer=CustomerModel(name=name),
        items=[ItemModel(product_name=product, quantity=qty, unit_price=price)],
    )


def test_fingerprint_is_order_insensitive():
    left = OrderModel(
        customer=CustomerModel(name="Existing Co"),
        items=[
            ItemModel(product_name="Keyboard", quantity=2),
            ItemModel(product_name="Mouse", quantity=1),
        ],
    )
    right = OrderModel(
        customer=CustomerModel(name="existing CO"),
        items=[
            ItemModel(product_name="mouse", quantity=1),
            ItemModel(product_name="KEYBOARD", quantity=2),
        ],
    )
    assert fingerprint_order(left) == fingerprint_order(right)


def test_duplicate_detected_within_window():
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    fp = fingerprint_order(_order())
    store = FakeStore(
        [
            {"order_id": "aaa", "fingerprint": fp, "created_at": recent},
            {"order_id": "bbb", "fingerprint": fp, "created_at": old},
        ]
    )
    detector = DuplicateDetector(store, window_hours=24)
    assert detector.find_duplicate(fp) == "aaa"


def test_no_duplicate_for_different_content():
    store = FakeStore([{"order_id": "aaa", "fingerprint": "other", "created_at": None}])
    detector = DuplicateDetector(store, window_hours=24)
    assert detector.find_duplicate(fingerprint_order(_order())) is None


def test_window_expiry_ignored():
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    fp = fingerprint_order(_order())
    detector = DuplicateDetector(FakeStore([{"order_id": "bbb", "fingerprint": fp, "created_at": old}]), window_hours=24)
    assert detector.find_duplicate(fp) is None
