"""Bare-name safety net: order-less text becomes a Yes/No question, not a junk order."""

from order_parser.channels.case_interactions import CaseInteractions
from order_parser.core.job_store import JobStore
from order_parser.core.pending_store import PendingStore


class StubPipeline:
    def __init__(self, pending_store):
        self.pending_store = pending_store
        self.resolver = None
        self.odoo = None
        self.calls = []

    def process(self, source, input_type, parsed, raw=None):
        self.calls.append((source, input_type, parsed, raw))
        raw = raw or {}
        self.pending_store.save({
            "order_id": "new-1", "job_id": raw.get("job_id"), "status": "review",
            "source": source, "parsed_order": parsed.model_dump(),
            "validation": {"customer": {"valid": True, "candidates": []}, "products": []},
            "resolution": {"blocking": [], "blocking_detail": [], "warnings": [],
                           "missing_information": [], "items": []},
            "raw": raw, "corrections": raw.get("corrections") or [],
            "overrides": {}, "created_at": "2026-10-05T10:00:00+00:00",
        })
        return {"status": "review", "order_id": "new-1"}


class StubOdoo:
    def list_uoms(self, limit=20):
        return [{"id": 1, "name": "Units"}]

    def list_sale_taxes(self, limit=20):
        return []


def _seed(job_store, pending_store):
    from order_parser.core.job import JobRecord, JobStatus

    job = JobRecord(job_id="ORD-20261005-000099", source="telegram", sender_id="8751097833",
                    input_type="text", status=JobStatus.NEEDS_REVIEW,
                    result={"status": "review", "order_id": "pend-99",
                            "customer": "ABC", "items": 1,
                            "resolution_blocked": ["CUSTOMER_UNRESOLVED"],
                            "customer_detail": {"raw_name": "ABC", "resolved": False,
                                                "partner_id": None, "partner_name": None}})
    job_store.save(job)
    pending_store.save({
        "order_id": "pend-99", "job_id": "ORD-20261005-000099", "status": "review",
        "source": "telegram",
        "parsed_order": {"order": {"customer": {"name": "ABC"}, "items": [],
                                   "metadata": {"source": "telegram"}},
                         "ai_response": {}, "extracted_text": ""},
        "validation": {"customer": {"valid": False, "reason": "x", "candidates": []},
                       "products": []},
        "resolution": {"blocking": ["CUSTOMER_UNRESOLVED"], "blocking_detail": [],
                       "warnings": [], "missing_information": [], "items": []},
        "raw": {"job_id": "ORD-20261005-000099"},
        "created_at": "2026-10-05T10:00:00+00:00",
    })
    return job


def _interactions(tmp_path):
    from order_parser.user_actions.awaiting import AwaitingStore

    job_store = JobStore(tmp_path / "jobs")
    pending_store = PendingStore(tmp_path / "pending")
    pipeline = StubPipeline(pending_store)
    pipeline.odoo = StubOdoo()
    interactions = CaseInteractions(pipeline, job_store,
                                    awaiting=AwaitingStore(tmp_path / "await"))
    return interactions, job_store, pending_store


def test_bare_name_triggers_safety_question(tmp_path):
    interactions, job_store, _ps = _interactions(tmp_path)
    _seed(job_store, interactions.pending_store)
    outcome = interactions.maybe_safety_net("8751097833", "Only Coffee")
    assert outcome is not None
    assert "Only Coffee" in outcome["text"]
    assert "ORD-20261005-000099" in outcome["text"]


def test_text_with_digits_skips_safety_net(tmp_path):
    interactions, job_store, _ps = _interactions(tmp_path)
    _seed(job_store, interactions.pending_store)
    assert interactions.maybe_safety_net("8751097833", "bread 20") is None
    assert interactions.maybe_safety_net("8751097833", "/status") is None


def test_no_open_case_no_question(tmp_path):
    interactions, _js, _ps = _interactions(tmp_path)
    assert interactions.maybe_safety_net("8751097833", "Only Coffee") is None


def test_safety_yes_applies_customer(tmp_path):
    interactions, job_store, pending_store = _interactions(tmp_path)
    _seed(job_store, interactions.pending_store)
    asked = interactions.maybe_safety_net("8751097833", "Only Coffee")
    assert asked is not None
    from order_parser.user_actions import callbacks as cb

    answer = interactions.callback_action(
        cb.decode("case:ORD-20261005-000099:sy"), "8751097833")
    assert answer["toast"] == "Saved"
    record = pending_store.list()[0]
    assert record["parsed_order"]["order"]["customer"]["name"] == "Only Coffee"
    assert record["corrections"][0]["actor"] == "8751097833"


def test_safety_no_declines_without_order(tmp_path):
    interactions, job_store, pending_store = _interactions(tmp_path)
    _seed(job_store, interactions.pending_store)
    interactions.maybe_safety_net("8751097833", "Only Coffee")
    from order_parser.user_actions import callbacks as cb

    answer = interactions.callback_action(
        cb.decode("case:ORD-20261005-000099:sn"), "8751097833")
    assert "new message" in answer["text"]
    assert pending_store.get("pend-99") is not None
    # Question consumed: answering twice is stale-safe.
    again = interactions.callback_action(
        cb.decode("case:ORD-20261005-000099:sy"), "8751097833")
    assert "no longer" in again["text"] or "expired" in (again.get("toast") or "")
