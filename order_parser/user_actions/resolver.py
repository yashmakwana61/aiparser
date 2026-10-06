"""Build OrderCaseStatus + UserActionRequired lists from stored results.

Consumes only persisted artifacts: the job record, the pending record
(parsed_order incl. ai_response, validation with candidates, resolution
summary) and the pipeline result dict. No re-diagnosis, no guessing.
"""

from __future__ import annotations

from typing import Any

from order_parser.user_actions.models import (
    VERB_CANCEL_CASE,
    VERB_CONFIRM_CASE,
    VERB_DUP_CREATE,
    VERB_DUP_VIEW,
    VERB_ENTER_CUSTOMER,
    VERB_ENTER_PRODUCT,
    VERB_ENTER_QTY,
    VERB_ENTER_UOM,
    VERB_PICK_CUSTOMER,
    VERB_PICK_PRODUCT,
    VERB_PICK_TAX,
    VERB_PICK_UOM,
    VERB_PRICE_ODOO,
    VERB_PRICE_ORDER,
    VERB_RETRY_CASE,
    VERB_REVIEW_CASE,
    VERB_STATUS_CASE,
    ActionDefinition,
    Candidate,
    OrderCaseStatus,
    Problem,
    Solution,
    UserActionRequired,
    UserFacingState,
)
from order_parser.user_actions.registry import (
    INFRA_BLOCKING_CODES,
    WARNING_ACTIONABLE,
    lookup,
)

_INFRA_REASONS = ("odoo_unavailable", "catalog_unavailable", "resolver_error")


def _codes(result: dict[str, Any], resolution: dict[str, Any]) -> list[str]:
    # Occurrence order mirrors the resolver's item loop, so repeated codes
    # (e.g. two ambiguous items) must be preserved — per-occurrence
    # attribution happens in the builders. Final action dedup (by action id
    # + item) still collapses true duplicates.
    codes = list(result.get("resolution_blocked") or [])
    if not codes:
        codes = list(resolution.get("blocking") or [])
    return [str(code) for code in codes]


def _blocking_messages(resolution: dict[str, Any]) -> dict[str, str]:
    messages: dict[str, str] = {}
    for entry in resolution.get("blocking_detail") or []:
        if isinstance(entry, dict) and entry.get("code"):
            messages[str(entry["code"])] = str(entry.get("message") or "")
    return messages


def _ocr_error_code(parsed_order: dict[str, Any]) -> str | None:
    ai = (parsed_order or {}).get("ai_response") or {}
    if not isinstance(ai, dict):
        return None
    if ai.get("ocr_failed") and ai.get("error_code"):
        return str(ai["error_code"])
    return None


def map_user_state(
    job_status: str,
    result_status: str | None,
    codes: list[str],
    reason: str = "",
    cancelled: bool = False,
    error_code: str = "",
) -> UserFacingState:
    if cancelled or error_code == "USER_CANCELLED":
        return UserFacingState.CANCELLED
    # A completed job is the ground truth (sale order exists) even when its
    # stored result snapshot still says pending/review from an earlier round.
    if job_status == "COMPLETED":
        return UserFacingState.COMPLETED
    if result_status == "success":
        return UserFacingState.COMPLETED
    if result_status == "pending":
        return UserFacingState.WAITING_CONFIRMATION
    if job_status in ("RECEIVED", "QUEUED", "PROCESSING", "EXTRACTING",
                      "NORMALIZING", "VALIDATING", "RESOLVING", "RETRYING",
                      "READY_FOR_ODOO", "SENT_TO_ODOO"):
        if result_status in (None, ""):
            return UserFacingState.PROCESSING
    if result_status == "review":
        if any(c in INFRA_BLOCKING_CODES for c in codes) or reason in _INFRA_REASONS:
            return UserFacingState.TEMPORARY_FAILURE
        return UserFacingState.ACTION_REQUIRED
    if job_status == "FAILED":
        return UserFacingState.TEMPORARY_FAILURE
    if job_status == "NEEDS_REVIEW":
        return UserFacingState.ACTION_REQUIRED
    return UserFacingState.PROCESSING


def _candidate(label: str, ref: int, detail: str = "") -> Candidate:
    return Candidate(label=label[:60], ref=ref, detail=detail[:80])


def _candidate_label(name: str, candidate: dict[str, Any]) -> str:
    city = str(candidate.get("city") or "").strip()
    if city and city.lower() not in str(name or "").lower():
        return f"{name} – {city}"
    return str(name or "?")


def _customer_candidates(validation: dict[str, Any]) -> list[dict[str, Any]]:
    customer = validation.get("customer") or {}
    candidates = customer.get("candidates") or []
    return [c for c in candidates if isinstance(c, dict)][:5]


def _product_entries(validation: dict[str, Any]) -> list[dict[str, Any]]:
    products = validation.get("products") or []
    return [p for p in products if isinstance(p, dict)]


def _entry_reason(entry: dict[str, Any]) -> str:
    return str(entry.get("reason") or "")


def build_actions(
    case_id: str,
    result: dict[str, Any],
    validation: dict[str, Any],
    resolution: dict[str, Any],
    parsed_order: dict[str, Any],
    uom_options: list[str] | None = None,
    tax_options: list[dict[str, Any]] | None = None,
) -> list[UserActionRequired]:
    """One UserActionRequired per blocking/warning issue, each independently fixable."""
    actions: list[UserActionRequired] = []
    codes = _codes(result, resolution)
    messages = _blocking_messages(resolution)

    ocr_code = _ocr_error_code(parsed_order)
    if ocr_code and ocr_code not in codes:
        codes = [ocr_code] + codes

    used: dict[str, set[int]] = {}
    for code in codes:
        definition = lookup(code)
        builder = _BUILDERS.get(code)
        if builder is not None:
            action = builder(case_id, definition, result, validation, resolution, parsed_order,
                             messages.get(code, ""), uom_options, tax_options,
                             used.setdefault(code, set()))
            if action is not None:
                if action.problem.item_index is not None:
                    used[code].add(action.problem.item_index)
                actions.append(action)
            continue
        actions.append(_generic_action(case_id, code, definition, messages.get(code, "")))

    for warning in result.get("resolution_warnings") or resolution.get("warnings") or []:
        if warning in WARNING_ACTIONABLE:
            action = _warning_action(case_id, warning, result, validation, resolution,
                                     uom_options, tax_options)
            if action is not None:
                actions.append(action)

    # Deduplicate identical action ids, keeping order.
    seen, unique = set(), []
    for action in actions:
        first = action.actions[0] if action.actions else None
        key = ((first.action_id if first else action.problem.code),
               action.problem.item_index)
        if key not in seen:
            seen.add(key)
            unique.append(action)
    return unique


def _generic_action(case_id: str, code: str, definition, context: str) -> UserActionRequired:
    problem = Problem(
        code=code,
        title=definition.title,
        description=definition.explanation + (f" ({context})" if context else ""),
        severity=definition.severity,
        recoverable=definition.recoverable,
    )
    return UserActionRequired(
        case_id=case_id,
        problem=problem,
        solution=Solution(kind="review", instructions=definition.solution),
        actions=[
            ActionDefinition(action_id=f"issue-{code.lower()}", label="Review order",
                             verb=VERB_REVIEW_CASE),
            ActionDefinition(action_id=f"status-{code.lower()}", label="Check status",
                             verb=VERB_STATUS_CASE),
        ],
    )


# ------------------------------------------------------------ builders


def _customer_action(case_id, definition, result, validation, resolution, parsed_order,
                     context, uom_options, tax_options, used=None):
    customer_detail = result.get("customer_detail") or {}
    detected = str(result.get("customer") or customer_detail.get("raw_name") or "").strip()
    candidates = [
        _candidate(_candidate_label(str(c.get("partner_name") or c.get("name") or "?"), c), i,
                   f"match score {c.get('score')}" if c.get("score") is not None else "")
        for i, c in enumerate(_customer_candidates(validation))
    ]
    actions = [
        ActionDefinition(action_id="customer", label=f"Use: {c.label}", verb=VERB_PICK_CUSTOMER,
                         candidate_ref=c.ref, primary=(i == 0))
        for i, c in enumerate(candidates)
    ]
    actions.append(ActionDefinition(action_id="customer-type", label="Type exact customer name",
                                    verb=VERB_ENTER_CUSTOMER))
    problem = Problem(
        code=definition.code,
        title=definition.title,
        description=(f'Customer on the order: "{detected}". ' if detected else "") + definition.explanation,
        field="customer",
        detected_value=detected,
        candidates=candidates,
        severity=definition.severity,
        recoverable=True,
    )
    return UserActionRequired(case_id=case_id, problem=problem,
                              solution=Solution(kind="select", instructions=definition.solution),
                              actions=actions)


def _product_action_for(code):
    def build(case_id, definition, result, validation, resolution, parsed_order,
              context, uom_options, tax_options, used=None):
        items = result.get("items_detail") or []
        entries = _product_entries(validation)
        target = None
        # Match by evidence, not reason strings: ambiguous resolutions always
        # carry candidates; unresolved ones usually carry none.
        for index, entry in enumerate(entries):
            if index in (used or set()):
                continue
            if entry.get("valid"):
                continue
            has_candidates = bool(entry.get("candidates"))
            if code == "PRODUCT_AMBIGUOUS" and has_candidates:
                target = (index, entry)
                break
            if code == "PRODUCT_UNRESOLVED" and not has_candidates:
                target = (index, entry)
                break
        if target is None:
            # Fall back to the first invalid line so the issue stays actionable.
            for index, entry in enumerate(entries):
                if index in (used or set()):
                    continue
                if not entry.get("valid"):
                    target = (index, entry)
                    break
        if target is None:
            return _generic_action(case_id, code, definition, context)
        index, entry = target
        raw_name = ""
        if index < len(items) and isinstance(items[index], dict):
            raw_name = str(items[index].get("product_name") or "")
        raw_name = raw_name or str(entry.get("product_name") or "")
        candidates = [
            _candidate(str(c.get("product_name") or c.get("name") or "?"), i,
                       f"score {c.get('score')}" if c.get("score") is not None else "")
            for i, c in enumerate(entry.get("candidates") or []) if isinstance(c, dict)
        ][:5]
        actions = [
            ActionDefinition(action_id=f"item-{index}-product", label=f"Use: {c.label}",
                             verb=VERB_PICK_PRODUCT, item_index=index, candidate_ref=c.ref,
                             primary=(i == 0))
            for i, c in enumerate(candidates)
        ]
        actions.append(ActionDefinition(action_id=f"item-{index}-product-type",
                                        label="Type exact product name",
                                        verb=VERB_ENTER_PRODUCT, item_index=index))
        problem = Problem(
            code=code,
            title=f"{definition.title} — item #{index + 1}",
            description=f'Requested product: "{raw_name}". ' + definition.explanation,
            field="product",
            item_index=index,
            detected_value=raw_name,
            candidates=candidates,
            severity=definition.severity,
            recoverable=True,
        )
        return UserActionRequired(case_id=case_id, problem=problem,
                                  solution=Solution(kind="select", instructions=definition.solution),
                                  actions=actions)
    return build


def _uom_action(case_id, definition, result, validation, resolution, parsed_order,
                context, uom_options, tax_options, used=None):
    summary_items = resolution.get("items") or []
    readiness = result.get("items_detail_readiness") or []
    target = None
    for index, item in enumerate(summary_items):
        if index in (used or set()):
            continue
        if not isinstance(item, dict):
            continue
        if item.get("uom_id") in (None, "") and (item.get("uom_method") in (None, "")):
            target = index
            break
    if target is None:
        for index, item in enumerate(readiness):
            if index in (used or set()):
                continue
            if isinstance(item, dict) and "uom" in (item.get("missing_fields") or []):
                target = index
                break
    if target is None:
        return _generic_action(case_id, definition.code, definition, context)
    detected, qty = "", ""
    items = result.get("items_detail") or []
    if target < len(items) and isinstance(items[target], dict):
        detected = str(items[target].get("uom") or "")
        qty = str(items[target].get("quantity") or "")
    options = list(uom_options or [])
    if detected and detected not in options:
        options = [detected] + options
    actions = [
        ActionDefinition(action_id=f"item-{target}-uom", label=f"Use: {opt}",
                         verb=VERB_PICK_UOM, item_index=target, candidate_ref=i,
                         primary=(i == 0))
        for i, opt in enumerate(options[:6])
    ]
    actions.append(ActionDefinition(action_id=f"item-{target}-uom-type", label="Type another unit",
                                    verb=VERB_ENTER_UOM, item_index=target))
    problem = Problem(
        code=definition.code,
        title=f"{definition.title} — item #{target + 1}",
        description=(f'Quantity: {qty}. Unit detected: "{detected}". ' if detected else "") + definition.explanation,
        field="uom",
        item_index=target,
        detected_value=detected,
        candidates=[_candidate(o, i) for i, o in enumerate(options[:6])],
        severity=definition.severity,
        recoverable=True,
    )
    return UserActionRequired(case_id=case_id, problem=problem,
                              solution=Solution(kind="select", instructions=definition.solution),
                              actions=actions)


def _quantity_action(case_id, definition, result, validation, resolution, parsed_order,
                     context, uom_options, tax_options, used=None):
    summary_items = resolution.get("items") or []
    items = result.get("items_detail") or []
    target = None
    for index, item in enumerate(summary_items):
        if index in (used or set()):
            continue
        if not isinstance(item, dict):
            continue
        try:
            qty = float(item.get("quantity_effective") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            target = index
            break
    if target is None:
        return _generic_action(case_id, definition.code, definition, context)
    name = ""
    if target < len(items) and isinstance(items[target], dict):
        name = str(items[target].get("product_name") or "")
    problem = Problem(
        code=definition.code,
        title=f"{definition.title} — item #{target + 1}",
        description=(f'Product: "{name}". ' if name else "") + definition.explanation,
        field="quantity",
        item_index=target,
        severity=definition.severity,
        recoverable=True,
    )
    return UserActionRequired(
        case_id=case_id, problem=problem,
        solution=Solution(kind="enter_number", instructions=definition.solution),
        actions=[ActionDefinition(action_id=f"item-{target}-qty", label="Enter quantity",
                                  verb=VERB_ENTER_QTY, item_index=target, primary=True)],
    )


def _price_missing_action(case_id, definition, result, validation, resolution, parsed_order,
                          context, uom_options, tax_options, used=None):
    summary_items = resolution.get("items") or []
    items = result.get("items_detail") or []
    target = None
    for index, item in enumerate(summary_items):
        if index in (used or set()):
            continue
        if isinstance(item, dict) and item.get("unit_price") is None and item.get("price_method") in (None, ""):
            target = index
            break
    if target is None:
        return _generic_action(case_id, definition.code, definition, context)
    name = ""
    if target < len(items) and isinstance(items[target], dict):
        name = str(items[target].get("product_name") or "")
    problem = Problem(
        code=definition.code,
        title=f"{definition.title} — item #{target + 1}",
        description=(f'Product: "{name}". ' if name else "") + definition.explanation,
        field="price",
        item_index=target,
        severity=definition.severity,
        recoverable=True,
    )
    return UserActionRequired(
        case_id=case_id, problem=problem,
        solution=Solution(kind="enter_number", instructions=definition.solution),
        actions=[ActionDefinition(action_id=f"item-{target}-price", label="Enter unit price",
                                  verb=VERB_ENTER_PRICE, item_index=target, primary=True)],
    )


def _deviation_action(case_id, definition, result, validation, resolution, parsed_order,
                      context, uom_options, tax_options, used=None):
    items = result.get("items_detail") or []
    summary_items = resolution.get("items") or []
    target = None
    for index, item in enumerate(summary_items):
        if index in (used or set()):
            continue
        if not isinstance(item, dict):
            continue
        if item.get("unit_price") is not None and index < len(items):
            target = index
            break
    if target is None:
        return None
    name, order_price = "", None
    if isinstance(items[target], dict):
        name = str(items[target].get("product_name") or "")
        order_price = items[target].get("unit_price")
    problem = Problem(
        code=definition.code,
        title=f"{definition.title} — item #{target + 1}",
        description=(f'Product: "{name}". Order price: {order_price}. ' if name else "") + definition.explanation,
        field="price",
        item_index=target,
        detected_value="" if order_price is None else str(order_price),
        severity="warning",
        recoverable=True,
    )
    return UserActionRequired(
        case_id=case_id, problem=problem,
        solution=Solution(kind="choose", instructions=definition.solution),
        actions=[
            ActionDefinition(action_id=f"item-{target}-price-order", label="Use order price",
                             verb=VERB_PRICE_ORDER, item_index=target, primary=True),
            ActionDefinition(action_id=f"item-{target}-price-odoo", label="Use Odoo price",
                             verb=VERB_PRICE_ODOO, item_index=target),
        ],
    )


def _tax_action_for(code):
    def build(case_id, definition, result, validation, resolution, parsed_order,
              context, uom_options, tax_options, used=None):
        summary_items = resolution.get("items") or []
        items = result.get("items_detail") or []
        target = None
        for index, item in enumerate(summary_items):
            if index in (used or set()):
                continue
            if isinstance(item, dict) and not (item.get("tax_ids") or []):
                target = index
                break
        if target is None and items:
            target = 0
        if target is None:
            return _generic_action(case_id, code, definition, context)
        name = ""
        if target < len(items) and isinstance(items[target], dict):
            name = str(items[target].get("product_name") or "")
        options = list(tax_options or [])[:6]
        actions = [
            ActionDefinition(action_id=f"item-{target}-tax", label=str(o.get("label") or o.get("name") or "?"),
                             verb=VERB_PICK_TAX, item_index=target, candidate_ref=i,
                             primary=(i == 0))
            for i, o in enumerate(options)
        ]
        problem = Problem(
            code=code,
            title=f"{definition.title} — item #{target + 1}",
            description=(f'Product: "{name}". ' if name else "") + definition.explanation,
            field="tax",
            item_index=target,
            candidates=[_candidate(str(o.get("label") or o.get("name") or "?"), i) for i, o in enumerate(options)],
            severity=definition.severity,
            recoverable=True,
        )
        return UserActionRequired(case_id=case_id, problem=problem,
                                  solution=Solution(kind="select", instructions=definition.solution),
                                  actions=actions)
    return build


def _duplicate_action(case_id, definition, result, validation, resolution, parsed_order,
                      context, uom_options, tax_options, used=None):
    existing = ""
    if context:
        existing = context.replace("Duplicate of recently ingested order ", "").strip()
    if not existing:
        existing = str(result.get("duplicate_of") or "").strip()
    problem = Problem(
        code=definition.code,
        title=definition.title,
        description=definition.explanation + (f" Existing order: {existing}." if existing else ""),
        field="order",
        detected_value=existing,
        severity="warning",
        recoverable=True,
    )
    return UserActionRequired(
        case_id=case_id, problem=problem,
        solution=Solution(kind="confirm", instructions=definition.solution),
        actions=[
            ActionDefinition(action_id="dup-view", label="View existing order", verb=VERB_DUP_VIEW),
            ActionDefinition(action_id="dup-create", label="Create anyway", verb=VERB_DUP_CREATE,
                             dangerous=True),
            ActionDefinition(action_id="dup-cancel", label="Cancel", verb=VERB_CANCEL_CASE),
        ],
    )


def _infra_action_for(code):
    def build(case_id, definition, result, validation, resolution, parsed_order,
              context, uom_options, tax_options, used=None):
        problem = Problem(code=code, title=definition.title, description=definition.explanation,
                          severity=definition.severity, recoverable=True)
        return UserActionRequired(
            case_id=case_id, problem=problem,
            solution=Solution(kind="retry", instructions=definition.solution),
            actions=[
                ActionDefinition(action_id="retry", label="Retry now", verb=VERB_RETRY_CASE, primary=True),
                ActionDefinition(action_id="status", label="Check status", verb=VERB_STATUS_CASE),
                ActionDefinition(action_id="cancel", label="Cancel", verb=VERB_CANCEL_CASE),
            ],
        )
    return build


def _upload_action_for(code):
    def build(case_id, definition, result, validation, resolution, parsed_order,
              context, uom_options, tax_options, used=None):
        problem = Problem(code=code, title=definition.title, description=definition.explanation,
                          severity=definition.severity, recoverable=True)
        return UserActionRequired(
            case_id=case_id, problem=problem,
            solution=Solution(kind="upload", instructions=definition.solution),
            actions=[
                ActionDefinition(action_id="status2", label="Check status", verb=VERB_STATUS_CASE),
                ActionDefinition(action_id="cancel2", label="Cancel", verb=VERB_CANCEL_CASE),
            ],
        )
    return build


_BUILDERS = {
    "CUSTOMER_UNRESOLVED": _customer_action,
    "CUSTOMER_AMBIGUOUS": _customer_action,
    "COLLECTOR_AS_CUSTOMER": _customer_action,
    "PRODUCT_AMBIGUOUS": _product_action_for("PRODUCT_AMBIGUOUS"),
    "PRODUCT_UNRESOLVED": _product_action_for("PRODUCT_UNRESOLVED"),
    "UOM_UNRESOLVED": _uom_action,
    "QUANTITY_CONFLICT": _quantity_action,
    "INVALID_QUANTITY": _quantity_action,
    "PRICE_MISSING": _price_missing_action,
    "PRICE_DEVIATION": _deviation_action,
    "TAX_UNRESOLVED": _tax_action_for("TAX_UNRESOLVED"),
    "TAX_CONFLICT": _tax_action_for("TAX_CONFLICT"),
    "TAX_MISSING": _tax_action_for("TAX_MISSING"),
    "DUPLICATE_ORDER": _duplicate_action,
    "ODOO_UNAVAILABLE": _infra_action_for("ODOO_UNAVAILABLE"),
    "ODOO_CREATE_FAILED": _infra_action_for("ODOO_CREATE_FAILED"),
    "SYS_TRANSIENT": _infra_action_for("SYS_TRANSIENT"),
    "INTERNAL_ERROR": _infra_action_for("INTERNAL_ERROR"),
    "OCR_UNAVAILABLE": _upload_action_for("OCR_UNAVAILABLE"),
    "OCR_EMPTY": _upload_action_for("OCR_EMPTY"),
    "AI_INTERPRETATION_FAILED": _upload_action_for("AI_INTERPRETATION_FAILED"),
    "INPUT_TOO_LARGE": _upload_action_for("INPUT_TOO_LARGE"),
    "INPUT_UNSUPPORTED": _upload_action_for("INPUT_UNSUPPORTED"),
}


def _warning_action(case_id, code, result, validation, resolution, uom_options, tax_options):
    definition = lookup(code)
    builder = _BUILDERS.get(code)
    if builder is None:
        return None
    return builder(case_id, definition, result, validation, resolution, {}, "", uom_options, tax_options)
