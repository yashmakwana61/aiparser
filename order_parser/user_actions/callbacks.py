"""Telegram callback protocol for order cases.

Format: ``case:<job_id>:<verb>[:<item_index>][:<candidate_ref>]``

Security properties (all enforced server-side, never trusted from chat):
- verb must be a known VERBS token;
- the job (case) must exist;
- the clicking user must own the case (telegram user id matches the job's
  sender/chat, tolerant of staff-id forms);
- the case must still be actionable (not completed/cancelled, pending
  record still present when the verb needs one);
- candidate_ref is an index into the server-side stored candidate list,
  never a name/id from the button.
"""

from __future__ import annotations

from dataclasses import dataclass

from order_parser.user_actions.models import (
    VERB_ENTER_PRICE,
    VERB_ENTER_PRODUCT,
    VERB_ENTER_QTY,
    VERB_ENTER_UOM,
    VERB_PICK_PRODUCT,
    VERB_PICK_TAX,
    VERB_PICK_UOM,
    VERB_PRICE_ODOO,
    VERB_PRICE_ORDER,
    VERBS,
)

# Verbs that address a specific order line; all others take at most a
# candidate/issue reference.
_ITEM_VERBS = {
    VERB_PICK_PRODUCT, VERB_ENTER_PRODUCT, VERB_PICK_UOM, VERB_ENTER_UOM,
    VERB_ENTER_QTY, VERB_ENTER_PRICE, VERB_PRICE_ORDER, VERB_PRICE_ODOO,
    VERB_PICK_TAX,
}


@dataclass
class ParsedCallback:
    case_id: str
    verb: str
    item_index: int | None = None
    candidate_ref: int | None = None


def encode(case_id: str, verb: str, item_index: int | None = None,
           candidate_ref: int | None = None) -> str:
    parts = ["case", case_id, verb]
    if item_index is not None:
        parts.append(str(item_index))
        if candidate_ref is not None:
            parts.append(str(candidate_ref))
    elif candidate_ref is not None:
        parts.extend(["", str(candidate_ref)])
    data = ":".join(parts)
    if len(data) > 64:
        raise ValueError("callback payload exceeds Telegram 64-byte limit")
    return data


def decode(data: str) -> ParsedCallback | None:
    if not data or not data.startswith("case:"):
        return None
    parts = data.split(":")
    if len(parts) < 3:
        return None
    _, case_id, verb, *rest = parts
    if not case_id or verb not in VERBS:
        return None
    item_index: int | None = None
    candidate_ref: int | None = None
    numbers: list[int] = []
    for token in rest:
        if token == "":
            continue
        try:
            numbers.append(int(token))
        except ValueError:
            return None
    if len(numbers) > 2:
        return None
    if verb in _ITEM_VERBS:
        if numbers:
            item_index = numbers[0]
        if len(numbers) > 1:
            candidate_ref = numbers[1]
    elif numbers:
        # Verbs without an item slot (customer pick, fix index, ...):
        # a lone number is the candidate/issue reference.
        candidate_ref = numbers[-1]
    return ParsedCallback(case_id=case_id, verb=verb,
                          item_index=item_index, candidate_ref=candidate_ref)


def _digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def owns_case(user_id: object, sender_id: str | None, chat_id: str | int | None = None) -> bool:
    """Tolerant ownership: matches telegram numeric ids and staff-id forms."""
    user_digits = _digits(user_id)
    if not user_digits:
        return False
    for candidate in (sender_id, chat_id):
        candidate_digits = _digits(candidate)
        if not candidate_digits:
            continue
        if user_digits == candidate_digits:
            return True
        # Staff ids like "user_8751097833" end with the telegram id.
        if candidate_digits.endswith(user_digits) or user_digits.endswith(candidate_digits):
            return True
    return False
