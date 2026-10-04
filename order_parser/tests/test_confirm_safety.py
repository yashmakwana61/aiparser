"""Confirm safety: one tap = one sale order; attachments never fail creation."""

import base64

from order_parser.config import Settings
from order_parser.core.pending_store import PendingStore
from order_parser.integrations.odoo_client import OdooClient
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.services.pipeline import OrderPipeline


class FakeOdoo:
    enabled = True

    def __init__(self):
        self.sale_creates = 0
        self.attachments = []
        self.fail_create = False

    def create_sale_order(self, partner_id, items, **kwargs):
        self.sale_creates += 1
        if self.fail_create:
            raise ConnectionError("odoo down")
        return {"id": 100 + self.sale_creates, "name": f"SO{100 + self.sale_creates:05d}"}

    def create_telegram_message(self, *args, **kwargs):
        return 1

    def upload_file_to_odoo(self, filename, data, res_model, res_id):
        coerced = OdooClient._coerce_file_bytes(data)
        if coerced is None:
            return None
        self.attachments.append((filename, coerced))
        return 999


def _pending_record(order_id="test-1", file_data=None):
    parsed = ParsedOrder(order=OrderModel(
        customer=CustomerModel(name="X Ltd"),
        items=[ItemModel(product_name="Bread", quantity=2, unit_price=10.0, uom="Units")],
        metadata=MetadataModel(confidence=99.0)))
    raw: dict = {"job_id": "ORD-TEST-1"}
    if file_data is not None:
        raw["file_data"] = {"filename": "order.pdf", "data": file_data}
    return {
        "order_id": order_id, "job_id": "ORD-TEST-1", "status": "pending",
        "source": "telegram", "parsed_order": parsed.model_dump(),
        "validation": {"customer": {"valid": True, "partner_id": 2690},
                       "products": [{"valid": True, "product_id": 165, "price": 10.0}]},
        "resolution": {"missing_information": []},
        "raw": raw, "fingerprint": "", "created_at": "2026-10-04T10:00:00+00:00",
    }


def _pipeline(tmp_path, odoo):
    pipeline = OrderPipeline(odoo=odoo, settings=Settings(log_dir=str(tmp_path / "logs")))
    pipeline.pending_store = PendingStore(tmp_path / "pending")
    pipeline.idempotency = None
    return pipeline


def test_double_confirm_creates_single_sale_order(tmp_path):
    odoo = FakeOdoo()
    pipeline = _pipeline(tmp_path, odoo)
    pipeline.pending_store.save(_pending_record())
    first = pipeline.confirm_order("test-1", actor="telegram")
    assert first["status"] == "success"
    second = pipeline.confirm_order("test-1", actor="telegram")
    assert second["status"] == "error"
    assert odoo.sale_creates == 1


def test_failed_confirm_restores_pending_record(tmp_path):
    odoo = FakeOdoo()
    odoo.fail_create = True
    pipeline = _pipeline(tmp_path, odoo)
    pipeline.pending_store.save(_pending_record())
    result = pipeline.confirm_order("test-1", actor="telegram")
    assert result["status"] == "error"
    assert pipeline.pending_store.get("test-1") is not None
    assert odoo.sale_creates == 1


def test_repr_bytes_attachment_uploads_fine(tmp_path):
    odoo = FakeOdoo()
    pipeline = _pipeline(tmp_path, odoo)
    pipeline.pending_store.save(_pending_record(file_data=repr(b"%PDF-binary-bytes")))
    result = pipeline.confirm_order("test-1", actor="telegram")
    assert result["status"] == "success"
    assert odoo.attachments and odoo.attachments[0][1] == b"%PDF-binary-bytes"


def test_upload_recovers_repr_bytes_and_skips_garbage(monkeypatch):
    captured = {}

    def fake_execute(model, method, args, kwargs=None):
        captured["datas"] = args[0]["datas"]
        return 4242

    client = OdooClient(url="http://x", db="d", username="u", password="p")
    monkeypatch.setattr(client, "execute_kw", fake_execute)
    assert client.upload_file_to_odoo("a.pdf", repr(b"ABC123"), "sale.order", 1) == 4242
    import base64 as _b64

    assert _b64.b64decode(captured["datas"]) == b"ABC123"
    assert client.upload_file_to_odoo("a.pdf", b"raw-bytes", "sale.order", 1) == 4242
    assert client.upload_file_to_odoo("a.pdf", "not-bytes-at-all", "sale.order", 1) is None


def test_upload_survives_transport_failure(monkeypatch):
    client = OdooClient(url="http://x", db="d", username="u", password="p")

    def boom(*args, **kwargs):
        raise ConnectionError("down")

    monkeypatch.setattr(client, "execute_kw", boom)
    assert client.upload_file_to_odoo("a.pdf", b"data", "sale.order", 1) is None
    assert base64.b64encode(b"x")
