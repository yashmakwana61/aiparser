"""User-facing order-case model: problem + solution + action definitions.

This layer never diagnoses — it translates internal resolution results into
a machine-readable, channel-agnostic action plan (Telegram renders it today;
a web UI or WhatsApp could consume the same objects tomorrow).
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import Enum


class UserFacingState(str, Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    READY = "READY"
    CREATING_ORDER = "CREATING_ORDER"
    COMPLETED = "COMPLETED"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Callback verbs. Compact tokens keep Telegram's 64-byte callback_data limit
# safe: "case:<job_id>:<verb>:<item>:<index>".
VERB_PICK_CUSTOMER = "cu"
VERB_ENTER_CUSTOMER = "cue"
VERB_PICK_PRODUCT = "pr"
VERB_ENTER_PRODUCT = "pre"
VERB_PICK_UOM = "uo"
VERB_ENTER_UOM = "uoe"
VERB_ENTER_QTY = "qt"
VERB_ENTER_PRICE = "px"
VERB_PRICE_ORDER = "pxo"
VERB_PRICE_ODOO = "pxe"
VERB_PICK_TAX = "tx"
VERB_DUP_VIEW = "duv"
VERB_DUP_CREATE = "duc"
VERB_CONFIRM_CASE = "cf"
VERB_CANCEL_CASE = "cx"
VERB_FIX = "fx"
VERB_REVIEW_CASE = "vw"
VERB_RETRY_CASE = "rt"
VERB_STATUS_CASE = "st"
VERB_ALIAS_ADD = "al"
VERB_WHY = "why"
VERB_SAFETY_YES = "sy"
VERB_SAFETY_NO = "sn"

VERBS = {
    VERB_PICK_CUSTOMER, VERB_ENTER_CUSTOMER, VERB_PICK_PRODUCT,
    VERB_ENTER_PRODUCT, VERB_PICK_UOM, VERB_ENTER_UOM, VERB_ENTER_QTY, VERB_ENTER_PRICE,
    VERB_PRICE_ORDER, VERB_PRICE_ODOO, VERB_PICK_TAX, VERB_DUP_VIEW,
    VERB_DUP_CREATE, VERB_CONFIRM_CASE, VERB_CANCEL_CASE, VERB_FIX,
    VERB_REVIEW_CASE, VERB_RETRY_CASE, VERB_STATUS_CASE, VERB_ALIAS_ADD,
    VERB_WHY, VERB_SAFETY_YES, VERB_SAFETY_NO,
}


@dataclass
class Candidate:
    """One selectable option. `ref` is an index into the server-side stored
    candidate list — never a trusted name/id from the chat."""

    label: str
    ref: int
    detail: str = ""


@dataclass
class Problem:
    code: str
    title: str
    description: str
    field: str = ""
    item_index: int | None = None
    detected_value: str = ""
    candidates: list[Candidate] = dc_field(default_factory=list)
    severity: str = "error"  # error | warning | info
    recoverable: bool = True


@dataclass
class Solution:
    kind: str  # select | enter_text | enter_number | choose | confirm | upload | retry | review
    instructions: str


@dataclass
class ActionDefinition:
    action_id: str  # stable id within the case, e.g. "customer", "item-2-product"
    label: str
    verb: str
    item_index: int | None = None
    candidate_ref: int | None = None
    primary: bool = False
    dangerous: bool = False


@dataclass
class UserActionRequired:
    case_id: str
    problem: Problem
    solution: Solution
    actions: list[ActionDefinition] = dc_field(default_factory=list)


@dataclass
class OrderCaseStatus:
    case_id: str
    user_state: UserFacingState
    job_status: str = ""
    order_id: str | None = None
    sales_order: str | None = None
    customer: str = ""
    customer_resolved: bool = False
    partner_name: str | None = None
    items_total: int = 0
    items_ready: int = 0
    issues: list[UserActionRequired] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)
    tally_note: str = ""
    support_reference: str = ""


@dataclass
class CorrectionRecord:
    field: str
    item_index: int | None
    original_value: str
    corrected_value: str
    actor: str
    timestamp: str
    source: str = "telegram"
    reason: str = ""
