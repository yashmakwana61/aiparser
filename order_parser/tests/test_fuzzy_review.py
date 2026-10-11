"""Fuzzy-below-confirm orders must surface a one-tap action, never a dead end.

Regression test for d50f97d6199f (Xero Degrees): every line resolved and
the user fixed all flagged issues, yet the order sat in review with zero
actionable issues because one fuzzy-only match (74.9) capped confidence
below the confirmation band. The resolver now emits FUZZY_REVIEW and the
case offers the suggested product for one tap.
"""
from __future__ import annotations

from order_parser.config import Settings
from order_parser.core.job_store import JobStore
from order_parser.core.pending_store import PendingStore
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.models import DETERMINISTIC_METHODS
from order_parser.resolution.order_resolver import OrderResolver
from order_parser.services.pipeline import OrderPipeline
from order_parser.user_actions.case import build_case_status
from order_parser.user_actions.models import VERB_ENTER_PRODUCT, VERB_PICK_PRODUCT, VERB_RETRY_CASE
from order_parser.user_actions.resolver import build_actions


CATALOG = [
    {"id": 2465, "name": "PANINI BREAD 200 GMS", "default_code": "", "list_price": 50.0,
     "uom_id": 1, "taxes_id": [32]},
    {"id": 2410, "name": "FOCACCIA BREAD 5\"X5\" 200 GMS", "default_code": "", "list_price": 75.0,
     "uom_id": 1, "taxes_id": [32]},
]
PARTNERS = {7: {"id": 7, "name": "Xero Degrees M-block", "email": "", "phone": ""}}


class FakeOdoo:
    enabled = True

    def fetch_product_catalog(self):
        return [dict(p) for p in CATALOG]

    def get_partner(self, partner_id):
        return PARTNERS.get(int(partner_id))

    def search_partners(self, domain, limit=2):
        return [{"id": 7, "name": "Xero Degrees M-block"}]

    def search_uoms(self, domain, limit=1):
        return []

    def find_uom(self, name):
        return None

    def get_uom(self, uom_id):
        return None

    def get_partner_pricelist(self, partner_id):
        return None

    def compute_pricelist_price(self, *args):
        return None

    def get_partner_fiscal_position(self, partner_id):
        return None

    def map_taxes_through_fiscal_position(self, fp, tax_ids):
        return list(tax_ids)

    def default_sale_taxes(self):
        return [32]

    def create_partner(self, customer):
        raise AssertionError("must never create partners in these tests")

    def create_sale_order(self, partner_id, items, notes=""):
        return {"id": 1, "name": "SO00001"}

    def create_telegram_message(self, *args, **kwargs):
        return {"id": 1}

    def list_uoms(self, limit=20):
        return [{"id": 1, "name": "Units"}]

    def list_sale_taxes(self, limit=20):
        return [{"id": 32, "name": "GST 18%", "amount": 18.0}]


class NullStore:
    def list(self, status=None):
        return []


def _settings(**overrides):
    defaults = {"auto_create_products": False, "auto_create_all_orders": False}
    defaults.update(overrides)
    return Settings(**defaults)


def _pipeline(tmp_path, settings=None):
    odoo = FakeOdoo()
    settings = settings or _settings()
    resolver = OrderResolver(odoo, catalog=CatalogProvider(odoo),
                             alias_store=AliasStore(tmp_path / "aliases"),
                             pending_store=NullStore(), settings=settings)
    pipeline = OrderPipeline(odoo, settings=settings, resolver=resolver)
    pipeline.pending_store = PendingStore(tmp_path / "pending")
    resolver.pending_store = pipeline.pending_store
    resolver.duplicates.pending_store = pipeline.pending_store
    return pipeline


def _parsed(customer="Xero Degrees M-block", product="Panini Bread"):
    return ParsedOrder(order=OrderModel(
        customer=CustomerModel(name=customer),
        items=[ItemModel(product_name=product, quantity=2, unit_price=50.0, uom="Units")],
        metadata=MetadataModel(confidence=95.0),
    ), ai_response={})


def test_fuzzy_below_confirm_parks_in_review_with_actionable_warning(tmp_path):
    result = _pipeline(tmp_path).process("telegram", "text", _parsed())
    assert result["status"] == "review"
    assert result["resolution_blocked"] == []
    assert "FUZZY_REVIEW" in result["resolution_warnings"]
    entry = result["items_detail_readiness"][0]
    assert entry["product_id"] == 2465  # matched, just uncertain


def test_fuzzy_above_confirm_stays_quiet(tmp_path):
    result = _pipeline(tmp_path).process("telegram", "text", _parsed(product="Panini Breads"))
    assert "FUZZY_REVIEW" not in result["resolution_warnings"]


def test_fuzzy_issue_offers_suggested_product_for_one_tap(tmp_path):
    pipeline = _pipeline(tmp_path)
    result = pipeline.process("telegram", "text", _parsed())
    record = pipeline.pending_store.get(result["order_id"])
    actions = build_actions("JOB-1", result, record["validation"], record["resolution"],
                            record["parsed_order"], [], [])
    fuzzy = [a for a in actions if a.problem.code == "FUZZY_REVIEW"]
    assert len(fuzzy) == 1
    issue = fuzzy[0]
    assert issue.problem.item_index == 0
    verbs = [a.verb for a in issue.actions]
    assert VERB_PICK_PRODUCT in verbs and VERB_ENTER_PRODUCT in verbs
    # The suggested candidate carries the exact catalog name: tapping it
    # re-resolves deterministically instead of looping back into review.
    suggested = issue.problem.candidates[0]
    fresh = pipeline.resolver.products.resolve(suggested.label)
    assert fresh.resolution_method in DETERMINISTIC_METHODS


def test_two_fuzzy_lines_yield_two_issues(tmp_path):
    pipeline = _pipeline(tmp_path)
    parsed = ParsedOrder(order=OrderModel(
        customer=CustomerModel(name="Xero Degrees M-block"),
        items=[ItemModel(product_name="Panini Bread", quantity=2, unit_price=50.0, uom="Units"),
               ItemModel(product_name="Focacia Bread", quantity=1, unit_price=75.0, uom="Units")],
        metadata=MetadataModel(confidence=95.0),
    ), ai_response={})
    result = pipeline.process("telegram", "text", parsed)
    record = pipeline.pending_store.get(result["order_id"])
    actions = build_actions("JOB-1", result, record["validation"], record["resolution"],
                            record["parsed_order"], [], [])
    fuzzy = sorted(a.problem.item_index for a in actions if a.problem.code == "FUZZY_REVIEW")
    assert len(fuzzy) >= 1
    assert len(set(fuzzy)) == len(fuzzy), "each fuzzy line gets its own issue"


def test_review_with_no_actionable_issue_gets_retry_fallback():
    actions = build_actions("JOB-1", {"status": "review"}, {"customer": {}, "products": []},
                            {}, {}, [], [])
    assert len(actions) == 1
    assert actions[0].problem.code == "MANUAL_REVIEW"
    assert actions[0].actions[0].verb == VERB_RETRY_CASE


def test_fallback_never_fires_for_pending_or_complete():
    assert build_actions("J", {"status": "pending"}, {"customer": {}, "products": []},
                         {}, {}, [], []) == []
    assert build_actions("J", {"status": "success"}, {"customer": {}, "products": []},
                         {}, {}, [], []) == []


def test_healed_case_shows_fuzzy_issue(tmp_path):
    from order_parser.core.job import JobRecord, JobStatus

    pipeline = _pipeline(tmp_path)
    result = pipeline.process("telegram", "text", _parsed())
    job_store = JobStore(tmp_path / "jobs")
    job_store.save(JobRecord(job_id="JOB-1", source="telegram", sender_id="1",
                             input_type="text", status=JobStatus.NEEDS_REVIEW,
                             result={**result, "job_id": "JOB-1"}, review_required=True))
    record = pipeline.pending_store.get(result["order_id"])
    status = build_case_status(job_store.get("JOB-1"), record,
                               {**result, "job_id": "JOB-1"}, [], [])
    assert str(status.user_state) == "UserFacingState.ACTION_REQUIRED"
    assert any(i.problem.code == "FUZZY_REVIEW" for i in status.issues)
