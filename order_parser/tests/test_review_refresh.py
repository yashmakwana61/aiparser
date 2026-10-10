"""Review opens re-check Odoo so manually added products appear as candidates."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from order_parser.core.job import JobRecord, JobStatus
from order_parser.core.job_store import JobStore
from order_parser.core.pending_store import PendingStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.product_resolver import ProductResolver
from order_parser.user_actions.corrections import CorrectionService


class FakeOdoo:
    """In-memory Odoo catalog that tests can grow (manual product creation)."""

    def __init__(self, products=None, fail=False):
        self._products = list(products or [])
        self.fail = fail
        self.fetches = 0

    def fetch_product_catalog(self):
        self.fetches += 1
        if self.fail:
            raise ConnectionError("odoo down")
        return [dict(p) for p in self._products]

    def list_uoms(self, limit=20):
        return [{"id": 1, "name": "Units"}]

    def list_sale_taxes(self, limit=20):
        return [{"id": 32, "name": "GST 18%", "amount": 18.0}]


def _product(pid, name):
    return {"id": pid, "name": name, "default_code": "", "list_price": 10.0,
            "uom_id": 1, "taxes_id": [32]}


def _parsed_dict(customer="ABC", product="Kulcha Special"):
    return {
        "customer": {"name": customer},
        "items": [{"product_name": product, "quantity": 2,
                   "unit_price": 10.0, "uom": "Units"}],
        "metadata": {"source": "telegram", "input_type": "text", "confidence": 90.0},
    }


def _seed(job_store, pending_store, job_id="JOB-1", order_id="ORD-1",
          product="Kulcha Special", candidates=None, job_status=JobStatus.NEEDS_REVIEW):
    result = {"status": "review", "order_id": order_id, "job_id": job_id,
              "customer": "ABC", "items": 1,
              "resolution_blocked": ["PRODUCT_UNRESOLVED"],
              "customer_detail": {"raw_name": "ABC", "resolved": True,
                                  "partner_id": 7, "partner_name": "ABC Ltd"}}
    job_store.save(JobRecord(job_id=job_id, source="telegram", sender_id="8751097833",
                             input_type="text", status=job_status,
                             result=result, review_required=True))
    pending_store.save({
        "order_id": order_id, "job_id": job_id, "status": "review", "source": "telegram",
        "parsed_order": {"order": _parsed_dict(product=product),
                         "ai_response": {}, "extracted_text": ""},
        "validation": {"customer": {"valid": True, "candidates": []},
                       "products": [{"valid": False, "reason": "no_candidate_above_cutoff",
                                     "product_name": product,
                                     "candidates": list(candidates or [])}]},
        "resolution": {"blocking": ["PRODUCT_UNRESOLVED"], "blocking_detail": [],
                       "warnings": [], "missing_information": [], "items": [],
                       "customer": {"partner_id": 7}},
        "raw": {"job_id": job_id}, "created_at": "2026-10-04T10:00:00+00:00",
    })


def _service(tmp_path, odoo):
    job_store = JobStore(tmp_path / "jobs")
    pending_store = PendingStore(tmp_path / "pending")
    catalog = CatalogProvider(odoo, ttl_seconds=3600)
    resolver = SimpleNamespace(products=ProductResolver(catalog, aliases=None))
    pipeline = SimpleNamespace(resolver=resolver, pending_store=pending_store, odoo=odoo)
    return CorrectionService(job_store, pending_store, pipeline), job_store, pending_store


def test_review_refresh_finds_manually_added_product(tmp_path):
    odoo = FakeOdoo([_product(1, "Breads")])
    service, job_store, pending_store = _service(tmp_path, odoo)
    _seed(job_store, pending_store)
    assert service.refresh_product_candidates("JOB-1") == 0  # nothing new yet

    odoo._products.append(_product(9, "Kulcha Special"))  # staff adds it in Odoo
    assert service.refresh_product_candidates("JOB-1") == 1

    record = pending_store.get("ORD-1")
    candidates = record["validation"]["products"][0]["candidates"]
    assert candidates and candidates[0]["product_id"] == 9
    # Display-only: the line still needs one tap, nothing auto-completed.
    assert record["validation"]["products"][0]["valid"] is False
    assert record["status"] == "review"


def test_review_refresh_noop_when_already_current(tmp_path):
    odoo = FakeOdoo([_product(9, "Kulcha Special")])
    service, job_store, pending_store = _service(tmp_path, odoo)
    _seed(job_store, pending_store,
          candidates=[{"product_id": 9, "product_name": "Kulcha Special",
                       "name": "Kulcha Special", "score": 100.0, "method": "exact_name"}])
    assert service.refresh_product_candidates("JOB-1") == 0


def test_review_refresh_noop_for_completed_or_missing_case(tmp_path):
    odoo = FakeOdoo([_product(9, "Kulcha Special")])
    service, job_store, pending_store = _service(tmp_path, odoo)
    _seed(job_store, pending_store, job_id="JOB-DONE", order_id="ORD-DONE",
          job_status=JobStatus.COMPLETED)
    assert service.refresh_product_candidates("JOB-DONE") == 0
    assert service.refresh_product_candidates("JOB-GHOST") == 0


def test_review_refresh_keeps_stored_candidates_when_odoo_down(tmp_path):
    odoo = FakeOdoo([_product(1, "Breads")])
    service, job_store, pending_store = _service(tmp_path, odoo)
    stored = [{"product_id": 1, "product_name": "Breads",
               "name": "Breads", "score": 80.0, "method": "fuzzy_match"}]
    _seed(job_store, pending_store, candidates=stored)
    odoo.fail = True
    assert service.refresh_product_candidates("JOB-1") == 0
    record = pending_store.get("ORD-1")
    assert record["validation"]["products"][0]["candidates"] == stored


def test_catalog_invalidate_keeps_snapshot_on_failed_fetch():
    odoo = FakeOdoo([_product(1, "Breads")])
    catalog = CatalogProvider(odoo, ttl_seconds=3600)
    assert len(catalog.products()) == 1
    catalog.invalidate()
    odoo.fail = True
    # Next access retries Odoo and surfaces the error, but the old
    # snapshot survives (unlike refresh(), which clears first).
    with pytest.raises(ConnectionError):
        catalog.products()
    assert len(catalog._products) == 1
    odoo.fail = False
    assert len(catalog.products()) == 1
    assert odoo.fetches >= 2


def test_review_refresh_skips_valid_lines_and_missing_names(tmp_path):
    odoo = FakeOdoo([_product(9, "Kulcha Special")])
    service, job_store, pending_store = _service(tmp_path, odoo)
    _seed(job_store, pending_store)
    record = pending_store.get("ORD-1")
    record["validation"]["products"].append({"valid": True, "product_id": 9,
                                             "candidates": []})
    pending_store.save(record)
    # Only the one invalid line is re-checked; the valid line is untouched.
    assert service.refresh_product_candidates("JOB-1") == 1
    record = pending_store.get("ORD-1")
    assert record["validation"]["products"][1] == {"valid": True, "product_id": 9,
                                                  "candidates": []}
