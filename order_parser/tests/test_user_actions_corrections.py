"""Correction service: patches, provenance, deterministic reprocessing."""

import pytest

from order_parser.core.job_store import JobStore
from order_parser.core.pending_store import PendingStore
from order_parser.user_actions.corrections import (
    CaseNotActionable,
    CaseNotFound,
    CorrectionService,
    InvalidCorrection,
)


def _parsed_dict(customer="ABC", items=None):
    return {
        "customer": {"name": customer},
        "items": items or [{"product_name": "Lappy", "quantity": 2,
                            "unit_price": 100.0, "uom": "Units"}],
        "metadata": {"source": "telegram", "input_type": "text", "confidence": 90.0},
    }


def _validation(customer_candidates=None, products=None):
    return {
        "customer": {"valid": False, "reason": "ambiguous_customer",
                     "candidates": customer_candidates or []},
        "products": products or [],
    }


def _seed(job_store, pending_store, job_id="ORD-20261004-000123", order_id="pend-uuid-1",
          customer="ABC", customer_candidates=None, status="review",
          result_extra=None, job_status=None):
    from order_parser.core.job import JobRecord, JobStatus

    result = {"status": status, "order_id": order_id, "job_id": job_id,
              "customer": customer, "items": 1, "resolution_blocked": ["CUSTOMER_AMBIGUOUS"],
              "customer_detail": {"raw_name": customer, "resolved": False,
                                  "partner_id": None, "partner_name": None}}
    result.update(result_extra or {})
    job = JobRecord(job_id=job_id, source="telegram", sender_id="8751097833",
                    input_type="text", status=job_status or JobStatus.NEEDS_REVIEW,
                    result=result, review_required=True)
    job_store.save(job)
    pending_store.save({
        "order_id": order_id, "job_id": job_id, "status": status, "source": "telegram",
        "parsed_order": {"order": _parsed_dict(customer), "ai_response": {}, "extracted_text": ""},
        "validation": _validation(customer_candidates),
        "resolution": {"blocking": ["CUSTOMER_AMBIGUOUS"], "blocking_detail": [],
                       "warnings": [], "missing_information": [], "items": []},
        "raw": {"job_id": job_id}, "created_at": "2026-10-04T10:00:00+00:00",
    })
    return job


class StubPipeline:
    def __init__(self):
        self.calls = []
        self.result = {"status": "review", "order_id": "new-uuid-9",
                       "resolution_blocked": [], "customer": "X", "items": 1,
                       "customer_detail": {"raw_name": "X", "resolved": True,
                                           "partner_id": 7, "partner_name": "X Ltd"}}
        self.pending_store = None
        self.resolver = None
        self.odoo = None

    def process(self, source, input_type, parsed, raw=None):
        self.calls.append((source, input_type, parsed, raw))
        raw = raw or {}
        self.pending_store.save({
            "order_id": "new-uuid-9", "job_id": raw.get("job_id"),
            "status": "review", "source": source,
            "parsed_order": parsed.model_dump(),
            "validation": {"customer": {"valid": True, "candidates": []}, "products": []},
            "resolution": {"blocking": [], "blocking_detail": [], "warnings": [],
                           "missing_information": [], "items": []},
            "raw": raw, "corrections": raw.get("corrections") or [],
            "overrides": raw.get("overrides") or {},
            "created_at": "2026-10-04T11:00:00+00:00",
        })
        return dict(self.result)


class StubOdoo:
    def list_uoms(self, limit=20):
        return [{"id": 1, "name": "Units"}, {"id": 2, "name": "Boxes"}]

    def list_sale_taxes(self, limit=20):
        return [{"id": 32, "name": "GST 18%", "amount": 18.0}]


def _service(tmp_path, pipeline=None):
    job_store = JobStore(tmp_path / "jobs")
    pending_store = PendingStore(tmp_path / "pending")
    pipeline = pipeline or StubPipeline()
    pipeline.pending_store = pending_store
    pipeline.odoo = StubOdoo()
    return CorrectionService(job_store, pending_store, pipeline), job_store, pending_store


def test_customer_pick_patches_and_records_provenance(tmp_path):
    service, job_store, pending_store = _service(tmp_path)
    _seed(job_store, pending_store, customer_candidates=[
        {"partner_id": 7, "partner_name": "X Ltd", "score": 100.0}])
    outcome = service.apply_pick("ORD-20261004-000123", "customer", None, 0, actor="8751097833")
    assert outcome["case_id"] == "ORD-20261004-000123"
    # New pending record holds the patched customer + provenance.
    records = pending_store.list()
    assert len(records) == 1 and records[0]["order_id"] == "new-uuid-9"
    assert records[0]["parsed_order"]["order"]["customer"]["name"] == "X Ltd"
    corrections = records[0]["corrections"]
    assert corrections[0]["field"] == "customer"
    assert corrections[0]["original_value"] == "ABC"
    assert corrections[0]["corrected_value"] == "X Ltd"
    assert corrections[0]["target"] == {"partner_id": 7}
    assert corrections[0]["actor"] == "8751097833"


def test_pick_rejects_stale_candidate_index(tmp_path):
    service, job_store, pending_store = _service(tmp_path)
    _seed(job_store, pending_store, customer_candidates=[
        {"partner_id": 7, "partner_name": "X Ltd"}])
    with pytest.raises(InvalidCorrection):
        service.apply_pick("ORD-20261004-000123", "customer", None, 5, actor="u")


def test_unknown_case_and_completed_case_rejected(tmp_path):
    service, job_store, pending_store = _service(tmp_path)
    with pytest.raises(CaseNotFound):
        service.apply_pick("ORD-20990101-000001", "customer", None, 0, actor="u")
    _seed(job_store, pending_store, job_status=None)
    from order_parser.core.job import JobStatus

    job = job_store.get("ORD-20261004-000123")
    job.status = JobStatus.COMPLETED
    job_store.save(job)
    with pytest.raises(CaseNotActionable):
        service.reprocess("ORD-20261004-000123", actor="u")


def test_quantity_text_must_be_numeric_positive(tmp_path):
    service, job_store, pending_store = _service(tmp_path)
    _seed(job_store, pending_store)
    with pytest.raises(InvalidCorrection):
        service.apply_text("ORD-20261004-000123", "quantity", 0, "twenty five", actor="u")
    with pytest.raises(InvalidCorrection):
        service.apply_text("ORD-20261004-000123", "quantity", 0, "-3", actor="u")


def test_reprocess_parks_record_and_updates_job(tmp_path):
    pipeline = StubPipeline()
    service, job_store, pending_store = _service(tmp_path, pipeline)
    _seed(job_store, pending_store)
    outcome = service.reprocess("ORD-20261004-000123", actor="u")
    assert outcome["result"]["order_id"] == "new-uuid-9"
    # Old pending file gone, parked-then-replaced by the re-run.
    assert pending_store.get("pend-uuid-1") is None
    job = job_store.get("ORD-20261004-000123")
    assert job.result["order_id"] == "new-uuid-9"


def test_cancel_case_deletes_pending(tmp_path):
    service, job_store, pending_store = _service(tmp_path)
    _seed(job_store, pending_store)
    assert service.cancel_case("ORD-20261004-000123", actor="u")["cancelled"] is True
    assert pending_store.get("pend-uuid-1") is None
    job = job_store.get("ORD-20261004-000123")
    assert job.error_code == "USER_CANCELLED"


def test_cancelled_case_renders_cancelled_state(tmp_path):
    from order_parser.user_actions.case import build_case_status

    service, job_store, pending_store = _service(tmp_path)
    _seed(job_store, pending_store)
    service.cancel_case("ORD-20261004-000123", actor="u")
    job = job_store.get("ORD-20261004-000123")
    status = build_case_status(job, None, dict(job.result or {}))
    assert status.user_state.value == "CANCELLED"
    assert status.issues == []


def test_mark_job_completed_flips_owning_job(tmp_path):
    from order_parser.core.job import JobStatus
    from order_parser.user_actions.case import mark_job_completed

    service, job_store, pending_store = _service(tmp_path)
    _seed(job_store, pending_store)
    assert mark_job_completed(job_store, "pend-uuid-1", "SO09999") is True
    job = job_store.get("ORD-20261004-000123")
    assert job.status == JobStatus.COMPLETED
    assert job.odoo_order_name == "SO09999"
    assert mark_job_completed(job_store, "no-such-order", None) is False


def test_duplicate_create_moves_to_confirmation(tmp_path):
    service, job_store, pending_store = _service(tmp_path)
    _seed(job_store, pending_store,
          result_extra={"resolution_blocked": ["DUPLICATE_ORDER"]})
    outcome = service.apply_duplicate_create("ORD-20261004-000123", actor="u")
    record = pending_store.get("pend-uuid-1")
    assert record["status"] == "pending"
    assert record["overrides"]["allow_duplicate"] is True
    assert "confirmation" in outcome["summary"].lower()


def test_alias_offer_uses_last_correction_target(tmp_path):
    from order_parser.resolution.alias_store import AliasStore

    pipeline = StubPipeline()
    service, job_store, pending_store = _service(tmp_path, pipeline)
    aliases = AliasStore(tmp_path / "aliases")
    pipeline.resolver = type("R", (), {"alias_store": aliases, "catalog": None})()
    _seed(job_store, pending_store, customer_candidates=[
        {"partner_id": 7, "partner_name": "X Ltd"}])
    service.apply_pick("ORD-20261004-000123", "customer", None, 0, actor="u")
    # Alias applies to the latest case record (new order id).
    latest = pending_store.list()[0]
    service.corrections = service  # no-op clarity
    out = service.apply_alias("ORD-20261004-000123", "customer", actor="u")
    assert out["summary"]
    from order_parser.resolution.normalization import normalize_name

    assert aliases.find_customer(normalize_name("ABC")) is not None
