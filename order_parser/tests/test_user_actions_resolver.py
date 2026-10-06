"""Action resolver: internal artifacts -> independent user actions."""

from order_parser.user_actions.models import UserFacingState
from order_parser.user_actions.resolver import build_actions, map_user_state


def _validation(customer_candidates=None, products=None):
    return {
        "customer": {"valid": not customer_candidates,
                     "reason": "ambiguous" if customer_candidates else "unresolved",
                     "candidates": customer_candidates or []},
        "products": products or [],
    }


def _product_entry(reason, candidates=None, valid=False, name="Lappy"):
    return {"product_name": name, "valid": valid, "reason": reason,
            "candidates": candidates or [], "product_id": None}


def test_customer_ambiguous_lists_candidates():
    actions = build_actions(
        "ORD-1", {"resolution_blocked": ["CUSTOMER_AMBIGUOUS"], "customer": "ABC",
                  "customer_detail": {"raw_name": "ABC"}},
        _validation(customer_candidates=[
            {"partner_id": 1, "name": "ABC Traders Pvt Ltd", "score": 100.0},
            {"partner_id": 2, "name": "ABC Trading Co.", "score": 99.0}]),
        {}, {})
    assert len(actions) == 1
    action = actions[0]
    assert action.problem.field == "customer"
    assert len(action.problem.candidates) == 2
    picks = [a for a in action.actions if a.verb == "cu"]
    assert len(picks) == 2 and picks[0].candidate_ref == 0
    assert any(a.verb == "cue" for a in action.actions)


def test_product_ambiguous_targets_item():
    actions = build_actions(
        "ORD-1",
        {"resolution_blocked": ["PRODUCT_AMBIGUOUS"],
         "items_detail": [{"product_name": "Lappy", "quantity": 2}]},
        _validation(products=[_product_entry("ambiguous_product", [
            {"product_id": 10, "name": 'Laptop 15"', "score": 95.0},
            {"product_id": 11, "name": "Lenovo Laptop", "score": 94.0}])]),
        {}, {})
    assert len(actions) == 1
    assert actions[0].problem.item_index == 0
    assert "Lappy" in actions[0].problem.description
    assert any(a.verb == "pr" and a.candidate_ref == 1 for a in actions[0].actions)


def test_product_fuzzy_tie_reason_targets_item_with_picks():
    # Real-world reason string from ProductResolver fuzzy near-ties.
    actions = build_actions(
        "ORD-1",
        {"resolution_blocked": ["PRODUCT_AMBIGUOUS"],
         "items_detail": [{"product_name": "Kulcha", "quantity": 12}]},
        {"customer": {"valid": True, "candidates": []},
         "products": [{"product_name": "Kulcha", "valid": False,
                       "reason": "fuzzy_candidates_too_close",
                       "candidates": [
                           {"product_id": 663, "name": "Golden Grain Kulcha Bread",
                            "score": 100.0},
                           {"product_id": 753, "name": "KULCHA BREAD (6 Pcs)",
                            "score": 100.0}]}]},
        {}, {})
    assert len(actions) == 1
    problem = actions[0].problem
    assert problem.item_index == 0
    assert "Kulcha" in problem.description
    assert len(problem.candidates) == 2
    picks = [a for a in actions[0].actions if a.verb == "pr"]
    assert len(picks) == 2
    assert picks[0].candidate_ref == 0 and picks[0].item_index == 0


def test_repeated_product_code_yields_distinct_item_actions():
    actions = build_actions(
        "ORD-1",
        {"resolution_blocked": ["PRODUCT_AMBIGUOUS", "PRODUCT_AMBIGUOUS"],
         "items_detail": [{"product_name": "Alpha", "quantity": 1},
                          {"product_name": "Beta", "quantity": 2}]},
        {"customer": {"valid": True, "candidates": []},
         "products": [
             {"product_name": "Alpha", "valid": False, "reason": "x",
              "candidates": [{"product_id": 1, "name": "Alpha A", "score": 90.0}]},
             {"product_name": "Beta", "valid": False, "reason": "y",
              "candidates": [{"product_id": 2, "name": "Beta B", "score": 91.0}]}]},
        {}, {})
    assert len(actions) == 2
    assert [a.problem.item_index for a in actions] == [0, 1]
    assert "Alpha" in actions[0].problem.description
    assert "Beta" in actions[1].problem.description


def test_product_unresolved_without_candidates_still_actionable():
    actions = build_actions(        "ORD-1",
        {"resolution_blocked": ["PRODUCT_UNRESOLVED"],
         "items_detail": [{"product_name": "Unobtainium", "quantity": 1}]},
        _validation(products=[_product_entry("product_not_found")]),
        {}, {})
    assert len(actions) == 1
    assert any(a.verb == "pre" for a in actions[0].actions)


def test_quantity_missing_requests_number():
    actions = build_actions(
        "ORD-1", {"resolution_blocked": ["QUANTITY_CONFLICT"]},
        {}, {"items": [{"quantity_effective": 0}]},
        {"items_detail": [{"product_name": "Cement"}]})
    assert actions and actions[0].solution.kind == "enter_number"


def test_duplicate_offers_view_and_create_anyway():
    actions = build_actions(
        "ORD-1", {"resolution_blocked": ["DUPLICATE_ORDER"]},
        {}, {"blocking_detail": [
            {"code": "DUPLICATE_ORDER",
             "message": "Duplicate of recently ingested order abc123"}]},
        {})
    assert len(actions) == 1
    verbs = {a.verb for a in actions[0].actions}
    assert {"duv", "duc"} <= verbs
    assert any(a.dangerous for a in actions[0].actions if a.verb == "duc")


def test_infra_maps_to_retry():
    actions = build_actions("ORD-1", {"resolution_blocked": ["ODOO_UNAVAILABLE"]}, {}, {}, {})
    assert actions and actions[0].solution.kind == "retry"
    assert any(a.verb == "rt" for a in actions[0].actions)


def test_unknown_code_degrades_to_generic_review():
    actions = build_actions("ORD-1", {"resolution_blocked": ["FUTURE_CODE_XYZ"]}, {}, {}, {})
    assert len(actions) == 1
    assert "Traceback" not in actions[0].problem.description


def test_ocr_failure_maps_to_upload_guidance():
    actions = build_actions("ORD-1", {}, {}, {},
                            {"ai_response": {"ocr_failed": True, "error_code": "OCR_EMPTY"}})
    assert actions and actions[0].problem.code == "OCR_EMPTY"
    assert actions[0].solution.kind == "upload"


def test_price_deviation_is_optional_warning_action():
    actions = build_actions(
        "ORD-1", {"resolution_warnings": ["PRICE_DEVIATION"],
                  "items_detail": [{"product_name": "Laptop", "unit_price": 1500}]},
        {}, {"items": [{"unit_price": 1500}]}, {})
    assert len(actions) == 1
    assert actions[0].problem.severity == "warning"
    assert {a.verb for a in actions[0].actions} == {"pxo", "pxe"}


def test_actions_deduplicated():
    actions = build_actions(
        "ORD-1", {"resolution_blocked": ["CUSTOMER_UNRESOLVED", "CUSTOMER_UNRESOLVED"]},
        _validation(), {}, {})
    assert len(actions) == 1


def test_user_state_mapping():
    assert map_user_state("NEEDS_REVIEW", "pending", []) == UserFacingState.WAITING_CONFIRMATION
    assert map_user_state("NEEDS_REVIEW", "review", ["CUSTOMER_UNRESOLVED"]) == UserFacingState.ACTION_REQUIRED
    assert map_user_state("NEEDS_REVIEW", "review", ["ODOO_UNAVAILABLE"]) == UserFacingState.TEMPORARY_FAILURE
    assert map_user_state("COMPLETED", "success", []) == UserFacingState.COMPLETED
    assert map_user_state("FAILED", "error", []) == UserFacingState.TEMPORARY_FAILURE
    assert map_user_state("QUEUED", None, []) == UserFacingState.PROCESSING
    assert map_user_state("NEEDS_REVIEW", "review", [], cancelled=True) == UserFacingState.CANCELLED
