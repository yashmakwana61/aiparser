"""Assemble the user-facing Order Case from stored job + pending artifacts."""

from __future__ import annotations

from typing import Any

from order_parser.user_actions.models import OrderCaseStatus, UserFacingState
from order_parser.user_actions.resolver import build_actions, map_user_state


def _job_status(job) -> str:
    status = getattr(job, "status", "")
    return status.value if hasattr(status, "value") else str(status)


def mark_job_completed(job_store, order_id: str, sales_order: str | None) -> bool:
    """Flip the owning job to COMPLETED after a confirm-created sale order.

    Finds the job whose stored result points at the confirmed pending
    order. Best-effort: returns False when nothing matches.
    """
    if job_store is None or not order_id:
        return False
    try:
        jobs = job_store.list()
    except Exception:
        return False
    for job in jobs:
        result = getattr(job, "result", None)
        if isinstance(result, dict) and str(result.get("order_id") or "") == str(order_id):
            try:
                from order_parser.core.job import JobStatus

                job.status = JobStatus.COMPLETED
                job.review_required = False
                job.error_code = None
                if sales_order:
                    job.odoo_order_id = sales_order
                    job.odoo_order_name = sales_order
                    result["sales_order"] = sales_order
                    job.result = result
                job_store.save(job)
                return True
            except Exception:
                return False
    return False


def _result_status(result: dict[str, Any]) -> str | None:
    status = result.get("status")
    return str(status) if status else None


def _reason_text(resolution: dict[str, Any]) -> str:
    customer = (resolution.get("customer") or {}) if isinstance(resolution, dict) else {}
    if isinstance(customer, dict) and customer.get("reason"):
        return str(customer["reason"])
    return ""


def build_case_status(job, record: dict[str, Any] | None,
                      result: dict[str, Any] | None,
                      uom_options: list[str] | None = None,
                      tax_options: list[dict[str, Any]] | None = None,
                      cancelled: bool = False) -> OrderCaseStatus:
    result = dict(result or {})
    record = record or {}
    resolution = dict(record.get("resolution") or {})
    validation = dict(record.get("validation") or {})
    parsed_order = dict(record.get("parsed_order") or {})
    overrides = dict(record.get("overrides") or {})

    codes = list(result.get("resolution_blocked") or resolution.get("blocking") or [])
    job_error = str(getattr(job, "error_code", "") or "")
    state = map_user_state(_job_status(job),
                           _result_status(result), codes,
                           _reason_text(resolution), cancelled,
                           error_code=job_error)
    if record.get("status") == "pending" and state == UserFacingState.ACTION_REQUIRED:
        state = UserFacingState.WAITING_CONFIRMATION

    actions = build_actions(job.job_id, result, validation, resolution, parsed_order,
                            uom_options, tax_options)
    actions = _drop_accepted_deviations(actions, overrides, result)

    customer_detail = result.get("customer_detail") or {}
    customer = str(result.get("customer") or customer_detail.get("raw_name") or "")
    summary_items = resolution.get("items") or []
    items_total = len(summary_items) or int(result.get("items") or 0)
    items_ready = sum(1 for item in summary_items
                      if isinstance(item, dict) and item.get("status") == "resolved")
    if not summary_items and items_total:
        items_ready = items_total if state in (UserFacingState.COMPLETED,
                                               UserFacingState.WAITING_CONFIRMATION) else 0

    tally_note = ""
    if result.get("blocking_for_tally"):
        missing = result.get("missing_information") or []
        tally_note = ("Tally sync: pending — "
                      + ("missing " + ", ".join(missing) if missing else "financial data incomplete")
                      + ". The Odoo order reference above is final.")
    sales_order = result.get("sales_order")

    return OrderCaseStatus(
        case_id=job.job_id,
        user_state=state,
        job_status=_job_status(job),
        order_id=result.get("order_id") or record.get("order_id"),
        sales_order=str(sales_order) if sales_order else None,
        customer=customer,
        customer_resolved=bool(customer_detail.get("resolved")),
        partner_name=customer_detail.get("partner_name"),
        items_total=items_total,
        items_ready=items_ready,
        issues=actions if state in (UserFacingState.ACTION_REQUIRED,
                                    UserFacingState.TEMPORARY_FAILURE) else [],
        warnings=[str(w) for w in (result.get("resolution_warnings") or [])],
        tally_note=tally_note,
        support_reference=job.job_id,
    )


def _drop_accepted_deviations(actions, overrides: dict[str, Any],
                              result: dict[str, Any]):
    accepted = (overrides or {}).get("price_accepted") or {}
    if not accepted:
        return actions
    items = result.get("items_detail") or []
    kept = []
    for action in actions:
        if action.problem.code != "PRICE_DEVIATION" or action.problem.item_index is None:
            kept.append(action)
            continue
        key = str(action.problem.item_index)
        current = None
        if action.problem.item_index < len(items) and isinstance(items[action.problem.item_index], dict):
            current = items[action.problem.item_index].get("unit_price")
        try:
            if current is not None and float(current) == float(accepted.get(key)):
                continue
        except (TypeError, ValueError):
            pass
        kept.append(action)
    return kept
