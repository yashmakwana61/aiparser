"""Phase 16 hardening: deterministic, paginated review queue + store quarantine."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from order_parser.api import auth as auth_module
from order_parser.api.orders import router as orders_router
from order_parser.core import metrics
from order_parser.core.metrics import REGISTRY
from order_parser.core import pending_store as pending_store_module
from order_parser.core.pending_store import PendingStore


def record(order_id: str, created_at: str, status: str = "review") -> dict:
    return {"order_id": order_id, "created_at": created_at, "status": status}


@pytest.fixture(autouse=True)
def _isolate():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


# ------------------------------------------------------------ store listing


@pytest.mark.usefixtures("_isolate")
def test_listing_is_newest_first_regardless_of_write_order(tmp_path):
    store = PendingStore(tmp_path / "pending")
    store.save(record("ord_old", "2026-08-01T09:00:00+00:00"))
    store.save(record("ord_new", "2026-08-20T09:00:00+00:00"))
    store.save(record("ord_mid", "2026-08-10T09:00:00+00:00"))

    ids = [r["order_id"] for r in store.list()]

    assert ids == ["ord_new", "ord_mid", "ord_old"]


@pytest.mark.usefixtures("_isolate")
def test_records_without_timestamp_sort_last(tmp_path):
    store = PendingStore(tmp_path / "pending")
    store.save({"order_id": "ord_nodate"})
    store.save(record("ord_dated", "2026-01-01T00:00:00+00:00"))

    ids = [r["order_id"] for r in store.list()]

    assert ids == ["ord_dated", "ord_nodate"]


@pytest.mark.usefixtures("_isolate")
def test_pagination_window_slices_filtered_results(tmp_path):
    store = PendingStore(tmp_path / "pending")
    for day in range(1, 6):
        store.save(record(f"ord_{day}", f"2026-08-{day:02d}T00:00:00+00:00"))

    page_one = store.list(limit=2, offset=0)
    page_two = store.list(limit=2, offset=2)
    remainder = store.list(limit=2, offset=4)

    assert [r["order_id"] for r in page_one] == ["ord_5", "ord_4"]
    assert [r["order_id"] for r in page_two] == ["ord_3", "ord_2"]
    assert [r["order_id"] for r in remainder] == ["ord_1"]
    assert store.list(limit=2, offset=99) == []


@pytest.mark.usefixtures("_isolate")
def test_status_filter_applies_before_windowing(tmp_path):
    store = PendingStore(tmp_path / "pending")
    store.save(record("rev_new", "2026-08-05T00:00:00+00:00", status="review"))
    store.save(record("pen_new", "2026-08-04T00:00:00+00:00", status="pending"))
    store.save(record("rev_old", "2026-07-05T00:00:00+00:00", status="review"))

    page = store.list(status="review", limit=1)

    assert [r["order_id"] for r in page] == ["rev_new"]
    assert store.count(status="review") == 2


@pytest.mark.usefixtures("_isolate")
def test_limit_none_returns_everything_legacy_style(tmp_path):
    store = PendingStore(tmp_path / "pending")
    for day in range(1, 8):
        store.save(record(f"ord_{day}", f"2026-08-{day:02d}T00:00:00+00:00"))

    assert len(store.list()) == 7


@pytest.mark.usefixtures("_isolate")
def test_corrupt_file_quarantined_not_silently_skipped(tmp_path):
    store = PendingStore(tmp_path / "pending")
    store.save(record("ord_ok", "2026-08-02T00:00:00+00:00"))
    bad = tmp_path / "pending" / "ord_bad.json"
    bad.write_text("{broken json", encoding="utf-8")

    records = store.list()

    assert [r["order_id"] for r in records] == ["ord_ok"]
    assert not bad.exists()
    assert list((tmp_path / "pending").glob("ord_bad.json.corrupt-*"))
    assert REGISTRY.counter_value("pending_files_corrupt_total") >= 1


@pytest.mark.usefixtures("_isolate")
def test_unreadable_file_skipped_but_left_in_place(tmp_path, monkeypatch):
    store = PendingStore(tmp_path / "pending")
    store.save(record("ord_good", "2026-08-02T00:00:00+00:00"))
    unreadable = tmp_path / "pending" / "ord_locked.json"
    unreadable.write_text(json.dumps(record("ord_locked", "2026-08-03T00:00:00+00:00")), encoding="utf-8")

    real_loads = pending_store_module.json.loads

    def boom(payload, *args, **kwargs):
        if "ord_locked" in payload:
            raise OSError("permission denied")
        return real_loads(payload)

    monkeypatch.setattr(pending_store_module.json, "loads", boom)
    records = store.list()

    assert [r["order_id"] for r in records] == ["ord_good"]
    assert unreadable.exists(), "OSError files must be left for ops"


# ----------------------------------------------------------------- endpoint


class QueuePipeline:
    def __init__(self, store: PendingStore):
        self.pending_store = store


def build_client(store: PendingStore, monkeypatch) -> TestClient:
    class NoAuth:
        api_auth_token = ""

    monkeypatch.setattr(auth_module, "get_settings", lambda: NoAuth())
    app = FastAPI()
    app.state.pipeline = QueuePipeline(store)
    app.include_router(orders_router)
    return TestClient(app)


@pytest.mark.usefixtures("_isolate")
def test_list_endpoint_returns_envelope_with_defaults(tmp_path, monkeypatch):
    store = PendingStore(tmp_path / "pending")
    for day in range(1, 4):
        store.save(record(f"ord_{day}", f"2026-08-{day:02d}T00:00:00+00:00"))
    client = build_client(store, monkeypatch)

    response = client.get("/orders")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert [o["order_id"] for o in body["orders"]] == ["ord_3", "ord_2", "ord_1"]


@pytest.mark.usefixtures("_isolate")
def test_list_endpoint_paginates_and_reports_total(tmp_path, monkeypatch):
    store = PendingStore(tmp_path / "pending")
    for day in range(1, 6):
        store.save(record(f"ord_{day}", f"2026-08-{day:02d}T00:00:00+00:00"))
    client = build_client(store, monkeypatch)

    page_two = client.get("/orders", params={"limit": 2, "offset": 2}).json()

    assert [o["order_id"] for o in page_two["orders"]] == ["ord_3", "ord_2"]
    assert page_two["total"] == 5


@pytest.mark.usefixtures("_isolate")
def test_list_endpoint_rejects_invalid_paging_params(tmp_path, monkeypatch):
    client = build_client(PendingStore(tmp_path / "pending"), monkeypatch)

    assert client.get("/orders", params={"limit": 0}).status_code == 422
    assert client.get("/orders", params={"limit": 500}).status_code == 422
    assert client.get("/orders", params={"offset": -1}).status_code == 422


@pytest.mark.usefixtures("_isolate")
def test_get_order_found_and_missing(tmp_path, monkeypatch):
    store = PendingStore(tmp_path / "pending")
    store.save(record("ord_here", "2026-08-02T00:00:00+00:00"))
    client = build_client(store, monkeypatch)

    found = client.get("/orders/ord_here")
    missing = client.get("/orders/ord_ghost")

    assert found.status_code == 200
    assert found.json()["order"]["order_id"] == "ord_here"
    assert missing.status_code == 404
