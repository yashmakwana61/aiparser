"""Phase 6: persistent idempotency + duplicate protection."""
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from order_parser.config import Settings
from order_parser.core.idempotency_store import IdempotencyStore
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.resolution.duplicate_detector import fingerprint_order
from order_parser.services.pipeline import OrderPipeline

FP = "a" * 64


# ------------------------------------------------------------------- store


def _store(tmp_path, name="idem.sqlite3"):
    return IdempotencyStore(tmp_path / name)


def test_record_then_find_recent_within_window(tmp_path):
    store = _store(tmp_path)
    store.record(FP, status="success", order_ref="SO00001", source="telegram")
    recent = store.find_recent(FP, window_hours=24)
    assert recent and recent["order_ref"] == "SO00001" and recent["status"] == "success"


def test_find_recent_respects_window(tmp_path):
    store = _store(tmp_path)
    old_stamp = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    store.record(FP, status="success", order_ref="SO00009", created_at=old_stamp)
    assert store.find_recent(FP, window_hours=24) is None
    assert store.find_recent(FP, window_hours=48) is not None


def test_find_recent_empty_store_returns_none(tmp_path):
    assert _store(tmp_path).find_recent(FP) is None


def test_history_persists_across_instances(tmp_path):
    _store(tmp_path).record(FP, status="success", order_ref="SO1")
    reopened = _store(tmp_path)
    assert reopened.count() == 1
    assert reopened.find_recent(FP)["order_ref"] == "SO1"


def test_claim_is_exclusive_until_released(tmp_path):
    store = _store(tmp_path)
    assert store.claim(FP, owner="first") is True
    assert store.claim(FP, owner="second") is False
    store.release(FP)
    assert store.claim(FP, owner="third") is True


def test_stale_claim_reclaimed_after_ttl(tmp_path):
    store = _store(tmp_path)
    assert store.claim(FP, ttl_seconds=0.05, owner="crashed") is True
    time.sleep(0.08)
    assert store.claim(FP, ttl_seconds=0.05, owner="recovery") is True


def test_corrupt_db_degrades_without_raising(tmp_path):
    path = tmp_path / "broken.sqlite3"
    path.write_bytes(b"this is not a database")
    store = IdempotencyStore(path)
    assert store.find_recent(FP) is None
    assert store.claim(FP) is False
    store.record(FP)  # must not raise
    store.release(FP)


def test_purge_removes_only_old_history(tmp_path):
    store = _store(tmp_path)
    fresh = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    store.record(FP, created_at=old)
    store.record("b" * 64, status="success", created_at=fresh)
    removed = store.purge(older_than_days=30)
    assert removed == 1 and store.count() == 1


# -------------------------------------------------------------- fingerprint


def _order(customer="ABC Industries", items=None):
    return OrderModel(
        customer=CustomerModel(name=customer),
        items=items or [ItemModel(product_name="Bread", quantity=20)],
    )


def test_fingerprint_ignores_item_order_and_int_float_qty():
    a = fingerprint_order(_order(items=[ItemModel(product_name="Bread", quantity=20), ItemModel(product_name="Milk", quantity=2.0)]))
    b = fingerprint_order(_order(items=[ItemModel(product_name="Milk", quantity=2), ItemModel(product_name="Bread", quantity=20.0)]))
    assert a == b


def test_fingerprint_changes_with_quantity_and_customer():
    base = fingerprint_order(_order())
    more = fingerprint_order(_order(items=[ItemModel(product_name="Bread", quantity=25)]))
    other = fingerprint_order(_order(customer="Zeta Traders"))
    assert base != more and base != other


# ----------------------------------------------------------------- pipeline


class CountingOdoo:
    enabled = True

    def __init__(self, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first
        self.last_kwargs = {}

    def fetch_product_catalog(self):
        return [{"id": 1, "name": "Keyboard"}, {"id": 2, "name": "Mouse"}]

    def find_partner(self, customer):
        if customer.name == "Existing Co":
            return {"id": 42, "name": "Existing Co"}
        return None

    def create_partner(self, customer):
        return 99

    def create_sale_order(self, partner_id, items, notes="", client_order_ref=""):
        self.last_kwargs = {"client_order_ref": client_order_ref}
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("odoo down")
        return {"id": self.calls, "name": f"SO{self.calls:05d}"}


def _pipeline(odoo, tmp_path, **overrides):
    defaults = dict(
        auto_create_products=False,
        auto_create_all_orders=True,
        enable_idempotency=True,
        idempotency_db_path=str(tmp_path / "idem.sqlite3"),
        log_dir=str(tmp_path),
    )
    defaults.update(overrides)
    return OrderPipeline(odoo, settings=Settings(**defaults))


def _parsed(confidence=97.0):
    order = _order(items=[ItemModel(product_name="Keyboard", quantity=2)])
    order.metadata = MetadataModel(source="telegram", input_type="text", confidence=confidence)
    return ParsedOrder(order=order, extracted_text="po")


def test_duplicate_auto_submit_routed_to_review(tmp_path):
    odoo = CountingOdoo()
    pipeline = _pipeline(odoo, tmp_path)
    first = pipeline.process("telegram", "text", _parsed())
    assert first["status"] == "success"
    second = pipeline.process("email", "text", _parsed())
    assert second["status"] == "review"
    assert "Duplicate" in second["message"]
    assert second.get("duplicate_of") == "SO00001"
    assert odoo.calls == 1  # never created twice
    # fingerprint stamped on the ERP record for Odoo-side visibility
    assert len(odoo.last_kwargs["client_order_ref"]) == 16


def test_resubmit_allowed_after_window(tmp_path):
    odoo = CountingOdoo()
    pipeline = _pipeline(odoo, tmp_path, duplicate_window_hours=24)
    pipeline.process("telegram", "text", _parsed())
    # backdate the single history row beyond the window
    conn = sqlite3.connect(str(tmp_path / "idem.sqlite3"))
    conn.execute("UPDATE order_history SET created_at = ?", ["2020-01-01T00:00:00+00:00"])
    conn.commit()
    conn.close()
    again = pipeline.process("email", "text", _parsed())
    assert again["status"] == "success"
    assert odoo.calls == 2


def test_failed_creation_releases_claim_allowing_retry(tmp_path):
    odoo = CountingOdoo(fail_first=True)
    pipeline = _pipeline(odoo, tmp_path)
    failed = pipeline.process("telegram", "text", _parsed())
    assert failed["status"] == "error"
    retry = pipeline.process("telegram", "text", _parsed())
    assert retry["status"] == "success"
    assert odoo.calls == 2


def test_confirm_records_success_and_blocks_later_duplicates(tmp_path):
    odoo = CountingOdoo()
    pipeline = _pipeline(odoo, tmp_path, auto_create_all_orders=False)
    pending = pipeline.process("telegram", "text", _parsed(confidence=85))
    assert pending["status"] == "pending"
    confirmed = pipeline.confirm_order(pending["order_id"], actor="telegram")
    assert confirmed["status"] == "success"

    later_pending = pipeline.process("telegram", "text", _parsed(confidence=85))
    # Re-sent content is already blocked at ingestion: routed to review.
    assert later_pending["status"] == "review"
    assert later_pending["duplicate_of"] == "SO00001"
    blocked = pipeline.confirm_order(later_pending["order_id"])
    assert blocked["status"] == "error"
    assert "duplicate of recently ingested order SO00001" in blocked["message"]
    assert odoo.calls == 1


def test_disabled_flag_keeps_legacy_behavior(tmp_path):
    odoo = CountingOdoo()
    settings = Settings(auto_create_products=False, auto_create_all_orders=True, log_dir=str(tmp_path / "logs"))
    pipeline = OrderPipeline(odoo, settings=settings)
    assert pipeline.idempotency is None
    first = pipeline.process("telegram", "text", _parsed())
    second = pipeline.process("email", "text", _parsed())
    assert first["status"] == "success" and second["status"] == "success"
    assert odoo.calls == 2
