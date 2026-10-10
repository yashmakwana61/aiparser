"""Telegram orchestration for order cases (no network calls here).

All methods return plain render specs ``(text, keyboard, toast)``; the
channel handler performs the actual send/edit/answer with its retry
helpers. Fully unit-testable with fakes.
"""

from __future__ import annotations

from typing import Any

from order_parser.user_actions import callbacks as cb
from order_parser.user_actions.awaiting import AwaitingStore
from order_parser.user_actions.case import build_case_status
from order_parser.user_actions.corrections import (
    CaseNotActionable,
    CaseNotFound,
    CorrectionService,
    InvalidCorrection,
)
from order_parser.user_actions.models import (
    VERB_ALIAS_ADD,
    VERB_CANCEL_CASE,
    VERB_CONFIRM_CASE,
    VERB_DUP_CREATE,
    VERB_DUP_VIEW,
    VERB_ENTER_CUSTOMER,
    VERB_ENTER_PRICE,
    VERB_ENTER_PRODUCT,
    VERB_ENTER_QTY,
    VERB_ENTER_UOM,
    VERB_FIX,
    VERB_PICK_CUSTOMER,
    VERB_PICK_PRODUCT,
    VERB_PICK_TAX,
    VERB_PICK_UOM,
    VERB_PRICE_ODOO,
    VERB_PRICE_ORDER,
    VERB_RETRY_CASE,
    VERB_REVIEW_CASE,
    VERB_SAFETY_NO,
    VERB_SAFETY_YES,
    VERB_STATUS_CASE,
    VERB_WHY,
    OrderCaseStatus,
)
from order_parser.user_actions import renderer as R

_ENTER_KIND = {
    VERB_ENTER_CUSTOMER: "customer",
    VERB_ENTER_PRODUCT: "product",
    VERB_ENTER_UOM: "uom",
    VERB_ENTER_QTY: "quantity",
    VERB_ENTER_PRICE: "price",
}

_PICK_KIND = {
    VERB_PICK_CUSTOMER: "customer",
    VERB_PICK_PRODUCT: "product",
    VERB_PICK_UOM: "uom",
    VERB_PICK_TAX: "tax",
}


class CaseInteractions:
    """Binds jobs + pending records + corrections to Telegram renders."""

    def __init__(self, pipeline, job_store, awaiting: AwaitingStore | None = None) -> None:
        self.pipeline = pipeline
        self.job_store = job_store
        self.pending_store = getattr(pipeline, "pending_store", None)
        self.corrections = CorrectionService(
            job_store, self.pending_store, pipeline,
            getattr(pipeline, "resolver", None), getattr(pipeline, "odoo", None))
        self.awaiting = awaiting or AwaitingStore()

    # ------------------------------------------------------------ status

    def status(self, case_id: str) -> OrderCaseStatus | None:
        ctx = self.corrections.load_case(case_id)
        if ctx is None:
            return None
        return build_case_status(
            ctx["job"], ctx["record"], ctx["result"],
            uom_options=self.corrections.uom_options(),
            tax_options=self.corrections.tax_options())

    def latest_case_for_user(self, user_id: object) -> OrderCaseStatus | None:
        if self.job_store is None:
            return None
        try:
            jobs = self.job_store.list()
        except Exception:
            return None
        for job in jobs:
            if cb.owns_case(user_id, getattr(job, "sender_id", None)):
                return self.status(job.job_id)
        return None

    def render_for_result(self, result: dict[str, Any]) -> tuple[str, Any, str | None]:
        """First reply after processing: route by user-facing state."""
        case_id = str(result.get("job_id") or "")
        if not case_id:
            return ("Your order was received and is being processed.", None, None)
        status = self.status(case_id)
        if status is None:
            return ("Your order was received and is being processed.", None, None)
        from order_parser.user_actions.models import UserFacingState

        if status.user_state == UserFacingState.COMPLETED:
            return (*R.render_completion(status), None)
        if status.user_state == UserFacingState.WAITING_CONFIRMATION:
            text, keyboard = R.render_status(status)
            return (text, keyboard, None)
        return (*R.render_case(status), None)

    # ------------------------------------------------------------ callbacks

    def callback_action(self, parsed: cb.ParsedCallback, user_id: object) -> dict[str, Any]:
        """Dispatch a validated callback. Never raises for user errors."""
        ctx = self.corrections.load_case(parsed.case_id)
        if ctx is None:
            text, keyboard = R.render_invalid()
            return {"text": text, "keyboard": keyboard, "toast": None, "edit": False}
        job = ctx["job"]
        if not cb.owns_case(user_id, getattr(job, "sender_id", None)):
            text, _ = R.render_unauthorized()
            return {"text": text, "keyboard": None, "toast": "Not your order", "edit": False}
        try:
            return self._dispatch(parsed, str(user_id), ctx)
        except CaseNotFound:
            text, keyboard = R.render_stale()
            return {"text": text, "keyboard": keyboard, "toast": None, "edit": True}
        except CaseNotActionable as exc:
            text, keyboard = R.render_stale()
            return {"text": text, "keyboard": keyboard,
                    "toast": str(exc)[:150], "edit": True}
        except InvalidCorrection as exc:
            status = self.status(parsed.case_id)
            if status is None:
                text, keyboard = R.render_invalid()
                return {"text": text, "keyboard": keyboard, "toast": None, "edit": False}
            text, keyboard = R.render_case(status)
            return {"text": text, "keyboard": keyboard,
                    "toast": str(exc)[:150], "edit": True}

    def _dispatch(self, parsed: cb.ParsedCallback, actor: str, ctx: dict) -> dict[str, Any]:
        verb, case_id = parsed.verb, parsed.case_id
        if verb == VERB_FIX:
            return self._render_case_action(case_id, parsed.candidate_ref or 0)
        if verb in (VERB_REVIEW_CASE, VERB_WHY):
            return self._render_case_action(case_id, 0)
        if verb == VERB_STATUS_CASE:
            status = self._need_status(case_id)
            text, keyboard = R.render_status(status)
            return {"text": text, "keyboard": keyboard, "toast": None, "edit": True}
        if verb in _PICK_KIND:
            outcome = self.corrections.apply_pick(
                case_id, _PICK_KIND[verb], parsed.item_index,
                parsed.candidate_ref if parsed.candidate_ref is not None else -1,
                actor, self._validation(ctx))
            return self._after_correction(case_id, outcome)
        if verb in _ENTER_KIND:
            return self._start_input(case_id, verb, parsed.item_index, actor)
        if verb == VERB_PRICE_ORDER:
            outcome = self.corrections.apply_price_choice(
                case_id, parsed.item_index or 0, False, actor)
            return self._after_correction(case_id, outcome)
        if verb == VERB_PRICE_ODOO:
            outcome = self.corrections.apply_price_choice(
                case_id, parsed.item_index or 0, True, actor)
            return self._after_correction(case_id, outcome)
        if verb == VERB_DUP_VIEW:
            return self._dup_view(case_id)
        if verb == VERB_DUP_CREATE:
            outcome = self.corrections.apply_duplicate_create(case_id, actor)
            status = self._need_status(case_id)
            text, keyboard = R.render_status(status)
            return {"text": text, "keyboard": keyboard,
                    "toast": "Moved to confirmation", "edit": True}
        if verb == VERB_CONFIRM_CASE:
            return self._confirm(case_id, actor)
        if verb == VERB_CANCEL_CASE:
            self.corrections.cancel_case(case_id, actor)
            text, keyboard = R.render_cancelled(case_id)
            return {"text": text, "keyboard": keyboard, "toast": "Cancelled", "edit": True}
        if verb == VERB_RETRY_CASE:
            outcome = self.corrections.reprocess(case_id, actor)
            return self._after_correction(case_id, outcome, toast="Retried")
        if verb == VERB_SAFETY_YES:
            return self._safety_answer(case_id, actor, accept=True)
        if verb == VERB_SAFETY_NO:
            return self._safety_answer(case_id, actor, accept=False)
        if verb == VERB_ALIAS_ADD:
            return self._alias(case_id, actor)
        text, keyboard = R.render_invalid()
        return {"text": text, "keyboard": keyboard, "toast": None, "edit": False}

    # ------------------------------------------------------------ text input

    def maybe_safety_net(self, user_id: object, text: str) -> dict[str, Any] | None:
        """Bare-name safety net: order-less text + open customer issue = ask.

        Returns a render spec (and arms a one-shot Yes/No step), or None to
        let the text flow into normal order processing.
        """
        value = (text or "").strip()
        if not value or len(value) > 80 or value.startswith("/"):
            return None
        if any(ch.isdigit() for ch in value):
            return None
        status = self.latest_case_for_user(user_id)
        if status is None:
            return None
        from order_parser.user_actions.models import UserFacingState

        if status.user_state != UserFacingState.ACTION_REQUIRED:
            return None
        if not any(issue.problem.field == "customer" for issue in status.issues):
            return None
        self.awaiting.set(str(user_id), status.case_id, "safety", None, value=value)
        text_out, keyboard = R.render_safety_ask(status.case_id, value)
        return {"text": text_out, "keyboard": keyboard, "toast": None, "edit": False}

    def consume_text_input(self, user_id: object, text: str) -> dict[str, Any] | None:
        """Route a free-text message into a pending correction. None = new order."""
        pending = self.awaiting.peek(str(user_id))
        if pending is not None and pending.get("verb") == "safety":
            # Safety questions are answered with Yes/No buttons only; free
            # text falls through (it may re-arm the question below).
            return None
        awaiting = self.awaiting.pop(str(user_id))
        if not awaiting:
            return None
        case_id = str(awaiting.get("case_id") or "")
        verb = str(awaiting.get("verb") or "")
        item = awaiting.get("item_index")
        kind = _ENTER_KIND.get(verb)
        if not case_id or kind is None:
            return None
        try:
            outcome = self.corrections.apply_text(case_id, kind, item, text, str(user_id))
        except (CaseNotFound, CaseNotActionable):
            text_out, keyboard = R.render_stale()
            return {"text": text_out, "keyboard": keyboard, "toast": None, "edit": False}
        except InvalidCorrection as exc:
            return {"text": f"That value didn't work: {exc}\n\nSend the value again or tap Cancel on the order message.",
                    "keyboard": None, "toast": None, "edit": False}
        return self._after_correction(case_id, outcome, toast="Saved")

    # ------------------------------------------------------------ helpers

    def _validation(self, ctx: dict) -> dict[str, Any]:
        record = ctx.get("record") or {}
        validation = record.get("validation")
        return validation if isinstance(validation, dict) else {}

    def _need_status(self, case_id: str):
        status = self.status(case_id)
        if status is None:
            raise CaseNotFound(case_id)
        return status

    def _render_case_action(self, case_id: str, expanded: int) -> dict[str, Any]:
        # The stored candidates predate any product the user created in Odoo
        # after ingest; re-check Odoo now so the buttons offer it.
        try:
            self.corrections.refresh_product_candidates(case_id)
        except Exception:
            pass
        status = self._need_status(case_id)
        text, keyboard = R.render_case(status, expanded=max(0, expanded))
        return {"text": text, "keyboard": keyboard, "toast": None, "edit": True}

    def _after_correction(self, case_id: str, outcome: dict[str, Any],
                          toast: str | None = "Saved") -> dict[str, Any]:
        status = self._need_status(case_id)
        summary = str(outcome.get("summary") or "Correction saved.")
        text, keyboard = R.render_correction_applied(
            case_id, summary, len(status.issues))
        follow_up = None
        if self._alias_offerable(case_id):
            follow_up = self._alias_offer(case_id)
        return {"text": text, "keyboard": keyboard, "toast": toast,
                "edit": True, "follow_up": follow_up}

    def _alias_offerable(self, case_id: str) -> bool:
        ctx = self.corrections.load_case(case_id)
        if not ctx or not ctx.get("record"):
            return False
        corrections = (ctx["record"].get("corrections") or [])
        if not corrections:
            return False
        field = str((corrections[-1].get("field") or ""))
        return field == "customer" or field == "item.product_name"

    def _alias_offer(self, case_id: str) -> dict[str, Any] | None:
        ctx = self.corrections.load_case(case_id)
        if not ctx or not ctx.get("record"):
            return None
        last = (ctx["record"].get("corrections") or [])[-1]
        field = str(last.get("field") or "")
        kind = "customer" if field == "customer" else "product"
        raw = str(last.get("original_value") or "")
        corrected = str(last.get("corrected_value") or "")
        if not raw or not corrected:
            return None
        text, keyboard = R.render_alias_offer(case_id, raw, corrected, kind)
        return {"text": text, "keyboard": keyboard}

    def _safety_answer(self, case_id: str, actor: str, accept: bool) -> dict[str, Any]:
        entry = self.awaiting.pop(actor)
        if not entry or entry.get("verb") != "safety" or entry.get("case_id") != case_id:
            raise InvalidCorrection("that question has expired")
        value = str(entry.get("value") or "").strip()
        if not value:
            raise InvalidCorrection("that question has expired")
        if not accept:
            return {"text": "Okay — send your order as a new message any time.",
                    "keyboard": None, "toast": None, "edit": False}
        outcome = self.corrections.apply_text(case_id, "customer", None, value, actor)
        return self._after_correction(case_id, outcome, toast="Saved")

    def _alias(self, case_id: str, actor: str) -> dict[str, Any]:
        ctx = self.corrections.load_case(case_id)
        if not ctx or not ctx.get("record"):
            raise CaseNotFound(case_id)
        corrections = (ctx["record"].get("corrections") or [])
        if not corrections:
            raise InvalidCorrection("nothing to remember yet")
        field = str((corrections[-1].get("field") or ""))
        kind = "customer" if field == "customer" else "product"
        self.corrections.apply_alias(case_id, kind, actor)
        status = self._need_status(case_id)
        text, keyboard = R.render_status(status)
        return {"text": text, "keyboard": keyboard,
                "toast": "Remembered for next time", "edit": True}

    def _dup_view(self, case_id: str) -> dict[str, Any]:
        ctx = self.corrections.load_case(case_id)
        record = (ctx or {}).get("record") or {}
        detail = ""
        for entry in ((record.get("resolution") or {}).get("blocking_detail") or []):
            if isinstance(entry, dict) and entry.get("code") == "DUPLICATE_ORDER":
                detail = str(entry.get("message") or "")
        existing_id = detail.replace("Duplicate of recently ingested order ", "").strip()
        existing = self.pending_store.get(existing_id) if (self.pending_store and existing_id) else None
        if isinstance(existing, dict):
            parsed = existing.get("parsed_order") or {}
            customer = (parsed.get("customer") or {}).get("name", "?")
            items = parsed.get("items") or []
            lines = [f"Customer: {customer}", f"Items: {len(items)}",
                     f"Received: {existing.get('created_at', '?')}"]
            detail = f"Existing order `{existing_id}`\n" + "\n".join(lines)
        text, keyboard = R.render_duplicate_existing(case_id, detail or "Details are available in Odoo.")
        return {"text": text, "keyboard": keyboard, "toast": None, "edit": True}

    def _confirm(self, case_id: str, actor: str) -> dict[str, Any]:
        from order_parser.user_actions.case import mark_job_completed

        status = self._need_status(case_id)
        if not status.order_id:
            raise CaseNotActionable("nothing to confirm")
        result = self.pipeline.confirm_order(status.order_id, f"telegram:{actor}")
        if isinstance(result, dict) and result.get("status") == "success":
            mark_job_completed(self.job_store, status.order_id, result.get("sales_order"))
            fresh = self.status(case_id)
            if fresh is None:
                raise CaseNotFound(case_id)
            text, keyboard = R.render_completion(fresh)
            return {"text": text, "keyboard": keyboard, "toast": "Order created", "edit": True}
        fresh = self.status(case_id)
        if fresh is None:
            text, keyboard = R.render_invalid()
            return {"text": text, "keyboard": keyboard, "toast": None, "edit": False}
        text, keyboard = R.render_status(fresh)
        return {"text": text, "keyboard": keyboard,
                "toast": "Confirmation didn't complete", "edit": True}

    def _start_input(self, case_id: str, verb: str, item_index: int | None,
                     actor: str) -> dict[str, Any]:
        status = self._need_status(case_id)
        instruction = "Send the value."
        for issue in status.issues:
            for action in issue.actions:
                if action.verb == verb and action.item_index == item_index:
                    instruction = issue.solution.instructions
                    break
        self.awaiting.set(actor, case_id, verb, item_index)
        text, keyboard = R.render_input_prompt(case_id, instruction)
        return {"text": text, "keyboard": keyboard, "toast": None, "edit": False}
