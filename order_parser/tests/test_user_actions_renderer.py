"""Renderer: friendly Problem->Why->Solution->Action text, safe keyboards."""

from order_parser.user_actions.models import (
    ActionDefinition,
    Candidate,
    OrderCaseStatus,
    Problem,
    Solution,
    UserActionRequired,
    UserFacingState,
    VERB_PICK_PRODUCT,
)
from order_parser.user_actions import renderer as R


def _action(case_id="ORD-1"):
    return UserActionRequired(
        case_id=case_id,
        problem=Problem(code="PRODUCT_AMBIGUOUS", title="Product needs confirmation",
                        description='Requested product: "Lappy". Several products match.',
                        field="product", item_index=0, detected_value="Lappy",
                        candidates=[Candidate(label='Laptop 15"', ref=0)]),
        solution=Solution(kind="select", instructions="Select the correct product below."),
        actions=[ActionDefinition(action_id="item-0-product", label='Use: Laptop 15"',
                                  verb=VERB_PICK_PRODUCT, item_index=0, candidate_ref=0,
                                  primary=True)],
    )


def _status(**overrides):
    base = dict(case_id="ORD-20261004-000123", user_state=UserFacingState.ACTION_REQUIRED,
                customer="ABC", items_total=20, items_ready=18,
                issues=[_action()], support_reference="ORD-20261004-000123")
    base.update(overrides)
    return OrderCaseStatus(**base)


def test_case_render_has_structure_and_buttons():
    text, keyboard = R.render_case(_status())
    assert "Action required" in text
    assert "ORD-20261004-000123" in text
    assert "18 / 20 ready" in text
    assert "Lappy" in text
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert any(b.callback_data.startswith("case:ORD-20261004-000123:pr:0:0") for b in buttons)
    # No internal codes or tracebacks in user text.
    for banned in ("PRODUCT_AMBIGUOUS", "Traceback", "Exception", "/app/"):
        assert banned not in text


def test_case_render_expands_selected_issue():
    status = _status(issues=[_action(), _action()])
    text, _keyboard = R.render_case(status, expanded=1)
    assert "👉" in text


def test_completion_reports_tally_separately():
    text, _keyboard = R.render_completion(_status(
        user_state=UserFacingState.COMPLETED, sales_order="SO02481",
        tally_note="Tally sync: pending — missing tax. The Odoo order reference above is final."))
    assert "SO02481" in text
    assert "Tally sync: pending" in text
    assert "PRODUCT_AMBIGUOUS" not in text


def test_status_render_lists_issues():
    text, keyboard = R.render_status(_status())
    assert "1 remaining" in text
    assert any("Fix issues" in b.text for row in keyboard.inline_keyboard for b in row)


def test_stale_invalid_unauthorized_are_safe():
    stale, _kb = R.render_stale()
    assert "already been updated" in stale
    invalid, kb_none = R.render_invalid()
    assert kb_none is None and "Traceback" not in invalid
    denied, _ = R.render_unauthorized()
    assert "someone else" in denied


def test_long_text_truncated_to_telegram_limits():
    big = _action()
    big.problem.description = "x" * 9000
    text, _keyboard = R.render_case(_status(issues=[big]))
    assert len(text) <= 3800
