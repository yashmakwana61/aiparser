"""AI tiebreaker integration: picks apply, suggest re-ranks, failures degrade."""

from types import SimpleNamespace

from order_parser.config import Settings
from order_parser.models import CustomerModel, ItemModel, OrderModel
from order_parser.resolution.models import (
    BlockingIssue,
    CustomerResolution,
    ProductResolution,
    ResolvedItem,
    ResolutionStatus,
)
from order_parser.resolution.order_resolver import OrderResolver


def _settings(**overrides):
    base = dict(ai_match_enabled=True, ai_match_auto_threshold=95.0)
    base.update(overrides)
    return Settings(**base)


def _resolver(stub=None, **overrides):
    odoo = SimpleNamespace(enabled=False)
    return OrderResolver(odoo, catalog=None, alias_store=None, pending_store=None,
                         settings=_settings(**overrides), ai_matcher=stub)


def _product_ambiguous():
    return ProductResolution(
        raw_name="Widget", status=ResolutionStatus.AMBIGUOUS,
        candidates=[{"product_id": 1, "name": "Widget Alpha", "score": 90.0},
                    {"product_id": 2, "name": "Widget Beta", "score": 89.0}])


def _case():
    order = OrderModel(customer=CustomerModel(name="C"),
                       items=[ItemModel(product_name="Widget", quantity=1)])
    items = [ResolvedItem(index=0, item=order.items[0], quantity_effective=1.0,
                          product=_product_ambiguous())]
    blocking = [BlockingIssue(code="PRODUCT_AMBIGUOUS", message="x", item_index=0)]
    customer = CustomerResolution(status=ResolutionStatus.UNRESOLVED, reason="no_matching_customer")
    return order, items, blocking, customer


def _stub(picks=None, ranking=None, fail=False):
    calls = []

    def match_products(todo):
        calls.append(("products", todo))
        if fail:
            raise RuntimeError("gateway down")
        return picks or {}

    def rank_customers(name, candidates):
        calls.append(("customers", name))
        if fail:
            raise RuntimeError("gateway down")
        return ranking or []

    return SimpleNamespace(match_products=match_products, rank_customers=rank_customers,
                           model="gpt-4.1-mini", calls=calls)


def test_high_confidence_pick_resolves_and_unblocks():
    stub = _stub(picks={0: {"product_id": 2, "confidence": 97.0, "reason": "pack"}})
    resolver = _resolver(stub)
    order, items, blocking, customer = _case()
    resolver._apply_ai_matches(order, items, blocking, customer)
    assert items[0].product.status == ResolutionStatus.RESOLVED
    assert items[0].product.resolution_method == "ai_match"
    assert items[0].product.product_id == 2
    assert items[0].product.product_name == "Widget Beta"
    assert blocking == []
    assert items[0].product.details["ai_model"] == "gpt-4.1-mini"


def test_low_confidence_pick_only_reranks_buttons():
    stub = _stub(picks={0: {"product_id": 2, "confidence": 80.0, "reason": "maybe"}})
    resolver = _resolver(stub)
    order, items, blocking, customer = _case()
    resolver._apply_ai_matches(order, items, blocking, customer)
    assert items[0].product.status == ResolutionStatus.AMBIGUOUS
    assert len(blocking) == 1
    assert [c["product_id"] for c in items[0].product.candidates] == [2, 1]


def test_hallucinated_pick_ignored():
    stub = _stub(picks={0: {"product_id": 4242, "confidence": 99.0, "reason": "bogus"},
                        9: {"product_id": 1, "confidence": 99.0, "reason": "wrong item"}})
    resolver = _resolver(stub)
    order, items, blocking, customer = _case()
    resolver._apply_ai_matches(order, items, blocking, customer)
    assert items[0].product.status == ResolutionStatus.AMBIGUOUS
    assert len(blocking) == 1


def test_matcher_failure_degrades_silently():
    stub = _stub(fail=True)
    resolver = _resolver(stub)
    order, items, blocking, customer = _case()
    resolver._apply_ai_matches(order, items, blocking, customer)
    assert items[0].product.status == ResolutionStatus.AMBIGUOUS
    assert len(blocking) == 1


def test_disabled_matcher_never_called():
    stub = _stub(picks={0: {"product_id": 1, "confidence": 99.0, "reason": "x"}})
    resolver = _resolver(stub, ai_match_enabled=False)
    order, items, blocking, customer = _case()
    resolver._apply_ai_matches(order, items, blocking, customer)
    assert stub.calls == []
    assert len(blocking) == 1


def test_customer_rerank_reorders_without_accepting():
    stub = _stub(ranking=[8, 7])
    resolver = _resolver(stub)
    order, items, blocking, _customer = _case()
    customer = CustomerResolution(
        status=ResolutionStatus.AMBIGUOUS, reason="multiple_partners_match",
        candidates=[{"partner_id": 7, "name": "Alpha", "score": 90.0},
                    {"partner_id": 8, "name": "Beta", "score": 89.0}])
    resolver._apply_ai_matches(order, items, blocking, customer)
    assert customer.status == ResolutionStatus.AMBIGUOUS
    assert [c["partner_id"] for c in customer.candidates] == [8, 7]
    assert customer.details["ai_ranked"] is True
    assert blocking, "customer still needs a human tap"
