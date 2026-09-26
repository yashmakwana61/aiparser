"""Phase 8: observability - metrics, readiness, audit chain, request IDs."""
import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from order_parser.api import auth as auth_module
from order_parser.api import monitoring as monitoring_module
from order_parser.api.monitoring import install_http_metrics, router as monitoring_router
from order_parser.config import Settings
from order_parser.core import audit as audit_module
from order_parser.core import metrics as metrics_module
from order_parser.core.audit import verify_audit_chain, write_audit_entry
from order_parser.core.metrics import REGISTRY
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.services.pipeline import OrderPipeline


@pytest.fixture(autouse=True)
def _clean_metrics():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


@pytest.fixture
def audit_dir(tmp_path, monkeypatch):
    directory = tmp_path / "audit"
    monkeypatch.setattr(audit_module, "_chain_cache", {})
    return directory


# ------------------------------------------------------------------ metrics


def test_counter_increments_and_label_isolation():
    metrics_module.incr("demo_total", source="a")
    metrics_module.incr("demo_total", source="a")
    metrics_module.incr("demo_total", source="b")
    assert metrics_module.REGISTRY.counter_value("demo_total", source="a") == 2
    assert metrics_module.REGISTRY.counter_value("demo_total", source="b") == 1
    assert metrics_module.REGISTRY.counter_value("demo_total", source="c") == 0


def test_prometheus_rendering_escapes_labels():
    metrics_module.incr("weird_total", label='a"b\nc')
    rendered = REGISTRY.render_prometheus()
    assert "# TYPE weird_total counter" in rendered
    # quote and newline are escaped inside the label value
    assert 'label="a\\"b\\nc"' in rendered


def test_observe_renders_summary_series():
    metrics_module.observe("job_seconds", 0.5)
    metrics_module.observe("job_seconds", 1.5)
    stats = REGISTRY.observation("job_seconds")
    assert stats == {"count": 2.0, "sum": 2.0, "max": 1.5}
    rendered = REGISTRY.render_prometheus()
    assert "# TYPE job_seconds summary" in rendered
    assert "job_seconds_sum 2" in rendered
    assert "job_seconds_count 2" in rendered
    assert "job_seconds_max 1.500000" in rendered


# ----------------------------------------------------------------- pipeline


class CountingOdoo:
    enabled = True

    def fetch_product_catalog(self):
        return [{"id": 1, "name": "Keyboard"}]

    def find_partner(self, customer):
        return None

    def create_partner(self, customer):
        return 99

    def create_sale_order(self, partner_id, items, notes="", client_order_ref=""):
        return {"id": 1, "name": "SO00001"}


def _pipeline(tmp_path, **overrides):
    defaults = dict(
        auto_create_products=False,
        auto_create_all_orders=True,
        log_dir=str(tmp_path),
        idempotency_db_path=str(tmp_path / "idem.sqlite3"),
        enable_idempotency=True,
    )
    defaults.update(overrides)
    return OrderPipeline(CountingOdoo(), settings=Settings(**defaults))


def _parsed(confidence: float = 97.0):
    order = OrderModel(
        customer=CustomerModel(name="ABC"),
        items=[ItemModel(product_name="Keyboard", quantity=2)],
        metadata=MetadataModel(source="telegram", input_type="text", confidence=confidence),
    )
    return ParsedOrder(order=order)


def test_pipeline_success_increments_processed_and_duration(tmp_path):
    pipeline = _pipeline(tmp_path)
    result = pipeline.process("telegram", "text", _parsed())
    assert result["status"] == "success"
    assert REGISTRY.counter_value("orders_processed_total", source="telegram", status="success") == 1
    stats = REGISTRY.observation("orders_processing_seconds")
    assert stats and stats["count"] == 1


def test_duplicate_block_increments_metric(tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.process("email", "text", _parsed())
    second = pipeline.process("email", "text", _parsed())
    assert second["status"] == "review"
    assert REGISTRY.counter_value("duplicates_blocked_total", source="email") == 1
    assert REGISTRY.counter_value("orders_confirmed_total") == 0


def test_confirm_and_reject_counters(tmp_path):
    pipeline = _pipeline(tmp_path, auto_create_all_orders=False)
    pending = pipeline.process("telegram", "text", _parsed(confidence=85))
    assert pending["status"] == "pending"
    pipeline.confirm_order(pending["order_id"])
    assert REGISTRY.counter_value("orders_confirmed_total") == 1
    another = pipeline.process("telegram", "text", _parsed(confidence=85))  # duplicate -> review
    pipeline.reject_order(another["order_id"])
    assert REGISTRY.counter_value("orders_rejected_total") == 1


# --------------------------------------------------------------- api layer


def _mini_app(monkeypatch, token: str = "") -> TestClient:
    settings = Settings(api_auth_token=token)
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    monkeypatch.setattr(monitoring_module, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(monitoring_router)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    install_http_metrics(app)
    return TestClient(app)


def test_metrics_endpoint_public_without_token(monkeypatch):
    client = _mini_app(monkeypatch)
    assert client.get("/ping").status_code == 200  # generate traffic first
    body = client.get("/metrics").text
    assert "http_requests_total" in body
    assert "text/plain" in client.get("/metrics").headers["content-type"]


def test_metrics_endpoint_protected_with_token(monkeypatch):
    client = _mini_app(monkeypatch, token="secret")
    assert client.get("/metrics").status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200


def test_request_id_generated_and_echoed(monkeypatch):
    client = _mini_app(monkeypatch)
    response = client.get("/ping")
    rid = response.headers["x-request-id"]
    assert 8 <= len(rid) <= 32

    echoed = client.get("/ping", headers={"X-Request-ID": "my-correlation-id"})
    assert echoed.headers["x-request-id"] == "my-correlation-id"


def test_request_id_invalid_input_replaced(monkeypatch):
    client = _mini_app(monkeypatch)
    response = client.get("/ping", headers={"X-Request-ID": "bad id with spaces!" * 10})
    assert response.headers["x-request-id"] != ("bad id with spaces!" * 10)[:64]


def test_http_requests_counter_tagged_by_route_template(monkeypatch):
    client = _mini_app(monkeypatch)
    client.get("/ping")
    client.get("/ping")
    value = REGISTRY.counter_value(
        "http_requests_total", method="GET", path="/ping", status="200"
    )
    assert value == 2


def test_readyz_ok_with_unconfigured_integrations():
    app = FastAPI()
    app.include_router(monitoring_router)

    class Pipeline:
        odoo = type("O", (), {"enabled": False})()
        resolver = None
        idempotency = None
        pending_store = type("P", (), {"list": lambda self: []})()

    app.state.pipeline = Pipeline()
    client = TestClient(app)
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["components"]["odoo"] == "unconfigured"
    assert body["components"]["idempotency_store"] == "disabled"


def test_readyz_503_when_local_store_broken():
    app = FastAPI()
    app.include_router(monitoring_router)

    class Broken:
        def count(self):
            raise RuntimeError("db gone")

    class Pipeline:
        odoo = type("O", (), {"enabled": True})()
        resolver = object()
        idempotency = Broken()
        pending_store = type("P", (), {"list": lambda self: []})()

    app.state.pipeline = Pipeline()
    client = TestClient(app)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["components"]["idempotency_store"] == "error"


# ------------------------------------------------------------------- audit


def test_audit_chain_links_consecutive_entries(audit_dir):
    write_audit_entry({"order_id": "a"}, directory=audit_dir)
    write_audit_entry({"order_id": "b"}, directory=audit_dir)
    path = next(audit_dir.glob("*.jsonl"))
    breaks = verify_audit_chain(path)
    assert breaks == []
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[1]["prev_hash"] == lines[0]["hash"]


def test_audit_tamper_detection(audit_dir):
    write_audit_entry({"order_id": "a"}, directory=audit_dir)
    write_audit_entry({"order_id": "b"}, directory=audit_dir)
    path = next(audit_dir.glob("*.jsonl"))
    records = [json.loads(l) for l in path.read_text().splitlines()]
    records[0]["order_id"] = "tampered"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    breaks = verify_audit_chain(path)
    assert any("content hash mismatch" in b for b in breaks)


def test_audit_chain_continues_after_restart(audit_dir, monkeypatch):
    write_audit_entry({"order_id": "a"}, directory=audit_dir)
    monkeypatch.setattr(audit_module, "_chain_cache", {})  # simulate restart
    write_audit_entry({"order_id": "b"}, directory=audit_dir)
    path = next(audit_dir.glob("*.jsonl"))
    assert verify_audit_chain(path) == []


def test_audit_write_failure_swallowed_and_counted(tmp_path):
    before = REGISTRY.counter_value("audit_failures_total")
    blocker = tmp_path / "notdir"
    blocker.write_text("this is a file, not a directory")
    write_audit_entry({"order_id": "x"}, directory=blocker)
    assert REGISTRY.counter_value("audit_failures_total") == before + 1
