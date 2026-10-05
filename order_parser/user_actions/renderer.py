"""Telegram presentation for order cases: Problem -> Why -> Solution -> Action.

Pure functions: OrderCaseStatus in, (message text, InlineKeyboardMarkup) out.
No internal codes, no tracebacks, no secrets ever reach user text.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from order_parser.user_actions import callbacks as cb
from order_parser.user_actions.models import (
    VERB_ALIAS_ADD,
    VERB_CANCEL_CASE,
    VERB_CONFIRM_CASE,
    VERB_FIX,
    VERB_RETRY_CASE,
    VERB_REVIEW_CASE,
    VERB_SAFETY_NO,
    VERB_SAFETY_YES,
    VERB_STATUS_CASE,
    ActionDefinition,
    OrderCaseStatus,
    UserActionRequired,
    UserFacingState,
)

_STATE_HEADER = {
    UserFacingState.RECEIVED: "📦 Order received",
    UserFacingState.PROCESSING: "⏳ Processing your order",
    UserFacingState.ACTION_REQUIRED: "🔴 Action required",
    UserFacingState.WAITING_CONFIRMATION: "🟡 Order ready — confirmation needed",
    UserFacingState.READY: "✅ All issues resolved",
    UserFacingState.CREATING_ORDER: "⏳ Creating your order",
    UserFacingState.COMPLETED: "✅ Order created",
    UserFacingState.TEMPORARY_FAILURE: "🟠 Temporary system issue",
    UserFacingState.FAILED: "🔴 Order couldn't be processed",
    UserFacingState.CANCELLED: "✕ Order cancelled",
}

_MAX_TEXT = 3800


def _btn(label: str, case_id: str, verb: str, item=None, ref=None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label[:32], callback_data=cb.encode(case_id, verb, item, ref))


def _clip(text: str, limit: int = 300) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _issue_block(issue: UserActionRequired, expanded: bool) -> str:
    problem = issue.problem
    lines = [f"*{_clip(problem.title, 80)}*"]
    if problem.detected_value:
        lines.append(f"Found: `{_clip(problem.detected_value, 80)}`")
    if expanded:
        lines.append("")
        lines.append(_clip(problem.description, 500))
        lines.append("")
        lines.append(f"_{_clip(issue.solution.instructions, 300)}_")
    return "\n".join(lines)


def render_case(status: OrderCaseStatus, expanded: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    header = _STATE_HEADER.get(status.user_state, "📦 Order update")
    lines = [f"{header}", "", f"Order: `{status.case_id}`"]
    if status.customer:
        lines.append(f"Customer: {_clip(status.customer, 60)}")
    if status.items_total:
        lines.append(f"Items: {status.items_ready} / {status.items_total} ready")
    keyboard: list[list[InlineKeyboardButton]] = []

    if status.user_state == UserFacingState.COMPLETED:
        return render_completion(status)

    if not status.issues:
        lines += ["", "No pending issues found.", "",
                  f"Support reference: `{status.support_reference or status.case_id}`"]
        keyboard.append([_btn("Check status", status.case_id, VERB_STATUS_CASE)])
        return _pack(lines, keyboard)

    lines += ["", f"{len(status.issues)} issue(s) need your attention:", ""]
    for number, issue in enumerate(status.issues):
        is_expanded = number == expanded
        marker = "👉" if is_expanded else f"{number + 1}."
        lines.append(f"{marker} {_issue_block(issue, is_expanded)}")
        lines.append("")
        if not is_expanded:
            first = issue.actions[0] if issue.actions else None
            label = f"Fix: {(first.label if first else issue.problem.title)[:26]}"
            keyboard.append([_btn(label, status.case_id, VERB_FIX, None, number)])
        else:
            for action in issue.actions:
                keyboard.append([_btn(_action_label(action), status.case_id, action.verb,
                                      action.item_index, action.candidate_ref)])
    lines.append(f"Support reference: `{status.support_reference or status.case_id}`")
    keyboard.append([_btn("Review order", status.case_id, VERB_REVIEW_CASE),
                     _btn("Cancel", status.case_id, VERB_CANCEL_CASE)])
    return _pack(lines, keyboard)


def _action_label(action: ActionDefinition) -> str:
    prefix = "⚠️ " if action.dangerous else ""
    return f"{prefix}{action.label}"


def render_status(status: OrderCaseStatus) -> tuple[str, InlineKeyboardMarkup]:
    header = _STATE_HEADER.get(status.user_state, "📦 Order status")
    lines = [header, "", f"Order: `{status.case_id}`", f"Status: {status.user_state.value}"]
    if status.sales_order:
        lines.append(f"Odoo: `{status.sales_order}`")
    if status.customer:
        lines.append(f"Customer: {_clip(status.customer, 60)}")
    if status.items_total:
        lines.append(f"Items: {status.items_ready} / {status.items_total} ready")
    if status.issues:
        lines.append(f"Issues: {len(status.issues)} remaining")
        for number, issue in enumerate(status.issues[:5]):
            lines.append(f"{number + 1}. {_clip(issue.problem.title, 70)}")
    if status.tally_note:
        lines.append("")
        lines.append(_clip(status.tally_note, 200))
    lines += ["", f"Support reference: `{status.support_reference or status.case_id}`"]
    keyboard: list[list[InlineKeyboardButton]] = []
    if status.issues:
        keyboard.append([_btn("Fix issues", status.case_id, VERB_FIX, None, 0)])
    if status.user_state == UserFacingState.TEMPORARY_FAILURE:
        keyboard.append([_btn("Retry now", status.case_id, VERB_RETRY_CASE)])
    if status.user_state == UserFacingState.WAITING_CONFIRMATION and status.order_id:
        keyboard.append([_btn("Create order", status.case_id, VERB_CONFIRM_CASE)])
    return _pack(lines, keyboard)


def render_completion(status: OrderCaseStatus) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["✅ Order created", "", f"Order: `{status.case_id}`"]
    if status.sales_order:
        lines.append(f"Odoo: `{status.sales_order}`")
    if status.customer:
        lines.append(f"Customer: {_clip(status.customer, 60)}")
    if status.items_total:
        lines.append(f"Items: {status.items_total}")
    if status.tally_note:
        lines.append("")
        lines.append("Tally: " + _clip(status.tally_note, 200))
    else:
        lines += ["", "Tally: ready for sync (final state is confirmed in Odoo)."]
    keyboard = [[_btn("View status", status.case_id, VERB_STATUS_CASE)]]
    return _pack(lines, keyboard)


def render_input_prompt(case_id: str, what: str) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"✏️ {_clip(what, 100)}", "",
             "Reply to this message with the value.",
             "Or send any other message to start a new order instead."]
    keyboard = [[_btn("Cancel", case_id, VERB_CANCEL_CASE)]]
    return _pack(lines, keyboard)


def render_correction_applied(case_id: str, summary: str, remaining: int) -> tuple[str, InlineKeyboardMarkup]:
    if remaining <= 0:
        lines = ["✅ Correction saved.", "", _clip(summary, 300), "",
                 "All issues are resolved. The order is ready.",
                 "", f"Support reference: `{case_id}`"]
        keyboard = [[_btn("Review order", case_id, VERB_REVIEW_CASE),
                     _btn("Create order", case_id, VERB_CONFIRM_CASE)]]
    else:
        lines = ["✅ Correction saved.", "", _clip(summary, 300), "",
                 f"{remaining} issue(s) remaining.",
                 "", f"Support reference: `{case_id}`"]
        keyboard = [[_btn("Fix next issue", case_id, VERB_FIX, None, 0),
                     _btn("Review order", case_id, VERB_REVIEW_CASE)]]
    return _pack(lines, keyboard)


def render_alias_offer(case_id: str, raw_name: str, target_name: str, kind: str) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["💡 Remember this choice?", "",
             f'"{_clip(raw_name, 50)}" → "{_clip(target_name, 50)}"',
             "I'll recognise it automatically next time. Master data stays unchanged.",
             "", f"Support reference: `{case_id}`"]
    keyboard = [[_btn("Remember", case_id, VERB_ALIAS_ADD),
                 _btn("Not now", case_id, VERB_STATUS_CASE)]]
    _ = kind
    return _pack(lines, keyboard)


def render_safety_ask(case_id: str, value: str, hint: str = "") -> tuple[str, InlineKeyboardMarkup]:
    lines = [f'Is "{_clip(value, 60)}" the customer',
             f"for order `{case_id}`?",
             ""]
    if hint:
        lines.append(_clip(hint, 160))
        lines.append("")
    lines.append("Tap Yes and I'll attach it — or No to send a fresh order instead.")
    keyboard = [[_btn("Yes, use it", case_id, VERB_SAFETY_YES),
                 _btn("No, new order", case_id, VERB_SAFETY_NO)]]
    return _pack(lines, keyboard)


def render_stale() -> tuple[str, InlineKeyboardMarkup]:
    return ("This button is no longer valid — the order has already been updated.",
            InlineKeyboardMarkup([[InlineKeyboardButton("View current status", callback_data="stale:status")]]))


def render_invalid() -> tuple[str, None]:
    return ("Sorry, that action couldn't be understood. Please use the latest message buttons.", None)


def render_unauthorized() -> tuple[str, None]:
    return ("This order belongs to someone else, so you can't change it.", None)


def render_cancelled(case_id: str) -> tuple[str, InlineKeyboardMarkup]:
    return (f"✕ Order `{case_id}` cancelled. Send a new order any time.",
            InlineKeyboardMarkup([]))


def render_duplicate_existing(case_id: str, detail: str) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🔎 Existing order", "", _clip(detail, 600) or "Details are available in Odoo.",
             "", f"Support reference: `{case_id}`"]
    keyboard = [[_btn("Back", case_id, VERB_STATUS_CASE)]]
    return _pack(lines, keyboard)


def _pack(lines: list[str], keyboard: list[list[InlineKeyboardButton]]) -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join(lines)
    if len(text) > _MAX_TEXT:
        text = text[: _MAX_TEXT - 1] + "…"
    return text, InlineKeyboardMarkup(keyboard)
