"""Human correction service: patch -> deterministic re-resolve -> continue.

Never reruns AI extraction. The user's correction is authoritative for the
patched field; everything else re-resolves through the existing resolver
so safety gates stay intact. Original values are preserved in the pending
record's ``corrections`` list (provenance for audit/alias learning).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from order_parser.core.audit import write_audit_entry
from order_parser.models import ParsedOrder

logger = structlog.get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CaseNotFound(Exception):
    pass


class CaseNotActionable(Exception):
    pass


class InvalidCorrection(Exception):
    pass


class CorrectionService:
    """Applies Telegram corrections to a pending order case."""

    def __init__(self, job_store, pending_store, pipeline, resolver=None, odoo=None) -> None:
        self.job_store = job_store
        self.pending_store = pending_store
        self.pipeline = pipeline
        self.resolver = resolver or getattr(pipeline, "resolver", None)
        self.odoo = odoo or getattr(pipeline, "odoo", None)

    # ------------------------------------------------------------ lookup

    def load_case(self, case_id: str) -> dict[str, Any] | None:
        """Return case context (job, pending record, result...) or None."""
        job = self.job_store.get(case_id) if self.job_store is not None else None
        if job is None:
            return None
        result = dict(job.result or {})
        order_id = result.get("order_id")
        record = None
        if order_id and self.pending_store is not None:
            record = self.pending_store.get(str(order_id))
        if record is None and self.pending_store is not None:
            for candidate in self.pending_store.list():
                if candidate.get("job_id") == case_id:
                    record = candidate
                    break
        return {"job": job, "record": record, "result": result, "order_id": order_id}

    # ------------------------------------------------------------ options

    def uom_options(self, detected: str = "") -> list[str]:
        options: list[str] = []
        if self.odoo is not None:
            try:
                options = [str(u.get("name") or "") for u in (self.odoo.list_uoms(limit=20) or [])]
            except Exception:
                logger.exception("corrections.uom_options_failed")
                options = []
        options = [o for o in options if o]
        if detected and detected not in options:
            options = [detected] + options
        return options[:8]

    def tax_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        if self.odoo is not None:
            try:
                for tax in self.odoo.list_sale_taxes(limit=20) or []:
                    amount = tax.get("amount")
                    label = str(tax.get("name") or "")
                    if amount is not None:
                        try:
                            label = f"{label} ({float(amount):g}%)"
                        except (TypeError, ValueError):
                            pass
                    options.append({"id": tax.get("id"), "label": label,
                                    "name": str(tax.get("name") or "")})
            except Exception:
                logger.exception("corrections.tax_options_failed")
        return [o for o in options if o.get("id") is not None][:8]

    # ------------------------------------------------------------ apply

    def apply_pick(self, case_id: str, kind: str, item_index: int | None,
                   candidate_ref: int, actor: str,
                   validation: dict[str, Any] | None = None) -> dict[str, Any]:
        """Apply a candidate selection (customer/product/uom/tax pick)."""
        ctx = self._require_actionable(case_id)
        validation = validation if validation is not None else (ctx["record"].get("validation") or {})
        if kind == "customer":
            candidate = self._candidate(ctx, validation, "customer", None, candidate_ref)
            name = str(candidate.get("partner_name") or candidate.get("name") or "")
            partner_id = candidate.get("partner_id")
            if not name or partner_id is None:
                raise InvalidCorrection("selected customer has no partner")
            return self._reprocess(
                ctx, actor,
                patches={"customer_name": name},
                staged=[self._staged(
                    ctx, "customer", None, name, actor,
                    target={"partner_id": int(partner_id)})],
                summary=f'Customer set to "{name}".',
            )
        if kind == "product":
            index = self._require_item(ctx, item_index)
            candidate = self._candidate(ctx, validation, "product", index, candidate_ref)
            name = str(candidate.get("product_name") or candidate.get("name") or "")
            if not name:
                raise InvalidCorrection("selected product has no name")
            target: dict[str, Any] = {}
            if candidate.get("product_id") is not None:
                target["product_id"] = int(candidate["product_id"])
            return self._reprocess(
                ctx, actor,
                patches={"items": {index: {"product_name": name}}},
                staged=[self._staged(
                    ctx, "item.product_name", index, name, actor, target=target)],
                summary=f'Item #{index + 1} product set to "{name}".',
            )
        if kind == "uom":
            index = self._require_item(ctx, item_index)
            options = self.uom_options()
            if candidate_ref < 0 or candidate_ref >= len(options):
                raise InvalidCorrection("unit choice is no longer available")
            return self._reprocess(
                ctx, actor,
                patches={"items": {index: {"uom": options[candidate_ref]}}},
                staged=[self._staged(ctx, "item.uom", index, options[candidate_ref], actor)],
                summary=f'Item #{index + 1} unit set to "{options[candidate_ref]}".',
            )
        if kind == "tax":
            index = self._require_item(ctx, item_index)
            options = self.tax_options()
            if candidate_ref < 0 or candidate_ref >= len(options):
                raise InvalidCorrection("tax choice is no longer available")
            tax_id = int(options[candidate_ref]["id"])
            return self._reprocess(
                ctx, actor,
                patches={"items": {index: {"tax_ids": [tax_id]}}},
                staged=[self._staged(
                    ctx, "item.tax_ids", index, options[candidate_ref]["label"], actor,
                    target={"tax_id": tax_id})],
                summary=f'Item #{index + 1} tax set to "{options[candidate_ref]["label"]}".',
            )
        raise InvalidCorrection(f"unknown pick kind {kind!r}")

    def apply_text(self, case_id: str, kind: str, item_index: int | None,
                   text: str, actor: str) -> dict[str, Any]:
        """Apply a typed correction (names, quantities, prices)."""
        ctx = self._require_actionable(case_id)
        value = (text or "").strip()
        if not value:
            raise InvalidCorrection("empty value")
        if kind == "customer":
            return self._reprocess(
                ctx, actor, patches={"customer_name": value},
                staged=[self._staged(ctx, "customer", None, value, actor)],
                summary=f'Customer set to "{value}".')
        if kind == "product":
            index = self._require_item(ctx, item_index)
            return self._reprocess(
                ctx, actor, patches={"items": {index: {"product_name": value}}},
                staged=[self._staged(ctx, "item.product_name", index, value, actor)],
                summary=f'Item #{index + 1} product set to "{value}".')
        if kind == "uom":
            index = self._require_item(ctx, item_index)
            return self._reprocess(
                ctx, actor, patches={"items": {index: {"uom": value}}},
                staged=[self._staged(ctx, "item.uom", index, value, actor)],
                summary=f'Item #{index + 1} unit set to "{value}".')
        if kind == "quantity":
            index = self._require_item(ctx, item_index)
            number = self._parse_number(value, "quantity")
            return self._reprocess(
                ctx, actor, patches={"items": {index: {"quantity": number}}},
                staged=[self._staged(ctx, "item.quantity", index, str(number), actor)],
                summary=f'Item #{index + 1} quantity set to {number:g}.')
        if kind == "price":
            index = self._require_item(ctx, item_index)
            number = self._parse_number(value, "price")
            return self._reprocess(
                ctx, actor, patches={"items": {index: {"unit_price": number}}},
                staged=[self._staged(ctx, "item.unit_price", index, str(number), actor)],
                summary=f'Item #{index + 1} price set to {number:g}.')
        raise InvalidCorrection(f"unknown text kind {kind!r}")

    def apply_price_choice(self, case_id: str, item_index: int, use_odoo: bool,
                           actor: str) -> dict[str, Any]:
        """Resolve a price deviation by choosing the order or Odoo price."""
        ctx = self._require_actionable(case_id)
        record = ctx["record"]
        parsed = ParsedOrder.model_validate(record["parsed_order"])
        if item_index < 0 or item_index >= len(parsed.order.items):
            raise InvalidCorrection("item out of range")
        item = parsed.order.items[item_index]
        if use_odoo:
            reference = self._odoo_reference_price(record, item_index)
            if reference is None:
                raise InvalidCorrection("no Odoo reference price available")
            patches = {"items": {item_index: {"unit_price": float(reference)}}}
            staged = [self._staged(ctx, "item.unit_price", item_index,
                                   str(float(reference)), actor)]
            summary = f"Item #{item_index + 1} price set to Odoo price {float(reference):g}."
        else:
            if item.unit_price is None:
                raise InvalidCorrection("order has no price to keep")
            patches = {}
            staged = [self._staged(ctx, "item.unit_price", item_index,
                                   str(float(item.unit_price)), actor)]
            summary = f"Item #{item_index + 1} keeps order price {float(item.unit_price):g}."
        overrides = dict(record.get("overrides") or {})
        accepted = dict(overrides.get("price_accepted") or {})
        accepted[str(item_index)] = float(
            reference if use_odoo else item.unit_price)  # type: ignore[arg-type]
        overrides["price_accepted"] = accepted
        record["overrides"] = overrides
        return self._reprocess(ctx, actor, patches=patches, staged=staged, summary=summary)

    def apply_duplicate_create(self, case_id: str, actor: str) -> dict[str, Any]:
        """User explicitly accepts a possible duplicate: move to confirmation."""
        ctx = self._require_actionable(case_id)
        record = ctx["record"]
        overrides = dict(record.get("overrides") or {})
        overrides["allow_duplicate"] = True
        record["overrides"] = overrides
        record["status"] = "pending"
        record.setdefault("corrections", []).append({
            "field": "duplicate", "item_index": None,
            "original_value": "blocked", "corrected_value": "accepted-by-user",
            "actor": actor, "timestamp": _now_iso(), "source": "telegram",
            "reason": "user accepted possible duplicate", "target": {},
        })
        self.pending_store.save(record)
        self._audit(ctx, actor, "duplicate_create_accepted", {})
        return {"case_id": case_id, "summary": "Duplicate accepted — order moved to confirmation.",
                "status": self._fresh_status(ctx)}

    def apply_alias(self, case_id: str, kind: str, actor: str) -> dict[str, Any]:
        """Explicit alias-learning offer: remember the last correction."""
        ctx = self._require_actionable(case_id)
        record = ctx["record"]
        corrections = record.get("corrections") or []
        if not corrections:
            raise InvalidCorrection("nothing to remember yet")
        last = corrections[-1]
        raw = str(last.get("original_value") or "")
        target = last.get("target") or {}
        alias_store = getattr(self.resolver, "alias_store", None)
        if alias_store is None:
            alias_store = getattr(getattr(self.pipeline, "resolver", None), "alias_store", None)
        if alias_store is None:
            raise InvalidCorrection("alias store unavailable")
        if kind == "product" and target.get("product_id"):
            alias_store.create_product(raw, int(target["product_id"]), created_by=actor)
        elif kind == "customer" and target.get("partner_id"):
            alias_store.create_customer(raw, int(target["partner_id"]), created_by=actor)
        else:
            raise InvalidCorrection("last correction cannot become an alias")
        self._audit(ctx, actor, "alias_created", {"kind": kind, "raw": raw})
        return {"case_id": case_id, "summary": "Choice remembered for next time.",
                "status": self._fresh_status(ctx)}

    def reprocess(self, case_id: str, actor: str) -> dict[str, Any]:
        """Retry with no changes (temporary failures). Idempotency-safe."""
        ctx = self._require_actionable(case_id)
        return self._reprocess(ctx, actor, patches={}, staged=[],
                               summary="Retried with no changes.")

    def prepare_review(self, case_id: str) -> int:
        """Ready a case for a review open: heal stale state, refresh candidates.

        Repairs records parked as ``pending`` while blocking issues exist
        (pre-fix image orders could confirm-fail with zero actionable
        issues), then re-checks Odoo for products added after ingest.
        Returns the number of product lines with refreshed candidates.
        Never raises.
        """
        try:
            self.heal_stale_pending(case_id)
        except Exception:
            logger.exception("review.heal_failed", case_id=case_id)
        return self.refresh_product_candidates(case_id)

    def heal_stale_pending(self, case_id: str) -> bool:
        """Flip ``pending`` records with blocking issues back to ``review``.

        A pending record must be confirmable; when the stored resolution or
        result still carries blocking codes (e.g. CUSTOMER_UNRESOLVED) the
        case renders "awaits confirmation" with no issues and CONFIRM can
        only fail. Healing restores the actionable review state. Returns
        True when anything was changed. Never raises.
        """
        try:
            return self._heal_stale_pending(case_id)
        except Exception:
            logger.exception("review.heal_failed", case_id=case_id)
            return False

    def _heal_stale_pending(self, case_id: str) -> bool:
        from order_parser.core import metrics

        ctx = self.load_case(case_id)
        if ctx is None or ctx.get("record") is None:
            return False
        record = ctx["record"]
        if record.get("status") != "pending":
            return False
        resolution = record.get("resolution") or {}
        blocking = list(resolution.get("blocking") or []) if isinstance(resolution, dict) else []
        result = dict(ctx.get("result") or {})
        blocked = list(result.get("resolution_blocked") or [])
        if not blocking and not blocked:
            return False
        record["status"] = "review"
        if result.get("status") == "pending":
            result["status"] = "review"
        result["message"] = "Order sent for manual review."
        job = ctx["job"]
        try:
            job.result = result
            job.review_required = True
            job.review_reason = result["message"]
            self.job_store.save(job)
        except Exception:
            logger.exception("review.heal_job_save_failed", case_id=case_id)
            return False
        try:
            self.pending_store.save(record)
        except Exception:
            logger.exception("review.heal_record_save_failed", case_id=case_id)
            return False
        try:
            metrics.incr("review_stale_pending_healed_total")
        except Exception:
            pass
        logger.info("review.stale_pending_healed", case_id=case_id,
                    blocking=blocking or blocked)
        return True

    def refresh_product_candidates(self, case_id: str) -> int:
        """Re-check Odoo for unresolved/ambiguous product lines (review-read path).

        Staff often create the missing product in Odoo and then reopen the
        review: the stored validation candidates predate that change, so this
        re-resolves every still-invalid product line against a freshly
        fetched catalog and persists the new candidate lists into the pending
        record. Display-only: statuses, codes and resolutions are untouched —
        the user still confirms with one tap via the normal pick flow.

        Returns the number of lines whose candidates changed. Never raises:
        any failure (no record, completed case, Odoo down) keeps the stored
        candidates and returns 0.
        """
        try:
            return self._refresh_product_candidates(case_id)
        except Exception:
            logger.exception("review.refresh_candidates_failed", case_id=case_id)
            return 0

    def _refresh_product_candidates(self, case_id: str) -> int:
        from order_parser.core import metrics
        from order_parser.resolution.models import ResolutionStatus

        ctx = self.load_case(case_id)
        if ctx is None or ctx.get("record") is None:
            return 0
        job = ctx["job"]
        try:
            status_value = job.status.value if hasattr(job.status, "value") else str(job.status)
        except Exception:
            status_value = ""
        if status_value == "COMPLETED":
            return 0
        record = ctx["record"]
        resolver = self.resolver or getattr(self.pipeline, "resolver", None)
        product_resolver = getattr(resolver, "products", None)
        if product_resolver is None or self.pending_store is None:
            return 0
        try:
            parsed = ParsedOrder.model_validate(record["parsed_order"])
        except Exception:
            return 0
        validation = record.get("validation")
        if not isinstance(validation, dict):
            return 0
        entries = validation.get("products")
        if not isinstance(entries, list) or not entries:
            return 0
        items = list(parsed.order.items)
        if not items:
            return 0
        partner_id = None
        try:
            customer = (record.get("resolution") or {}).get("customer") or {}
            if isinstance(customer, dict) and customer.get("partner_id") is not None:
                partner_id = int(customer["partner_id"])
        except (TypeError, ValueError):
            partner_id = None

        catalog = getattr(product_resolver, "catalog", None)
        if catalog is not None and hasattr(catalog, "invalidate"):
            try:
                catalog.invalidate()
            except Exception:
                logger.exception("review.catalog_invalidate_failed", case_id=case_id)

        changed = 0
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or entry.get("valid"):
                continue
            if index >= len(items):
                continue
            raw_name = str(getattr(items[index], "product_name", "") or "").strip()
            if not raw_name:
                continue
            try:
                fresh = product_resolver.resolve(raw_name, partner_id)
            except Exception:
                logger.exception("review.resolve_failed", case_id=case_id,
                                 item_index=index, raw_name=raw_name)
                continue
            fresh_candidates = [
                {"product_id": c.get("product_id"),
                 "product_name": str(c.get("product_name") or c.get("name") or ""),
                 "name": str(c.get("name") or c.get("product_name") or ""),
                 "score": c.get("score"),
                 "method": c.get("method")}
                for c in (getattr(fresh, "candidates", None) or [])
                if isinstance(c, dict) and c.get("product_id") is not None
            ]
            if getattr(fresh, "status", None) == ResolutionStatus.RESOLVED and getattr(
                    fresh, "product_id", None) is not None:
                resolved_name = str(getattr(fresh, "product_name", "") or raw_name)
                fresh_candidates = [{"product_id": fresh.product_id,
                                     "product_name": resolved_name,
                                     "name": resolved_name,
                                     "score": getattr(fresh, "confidence", 100.0),
                                     "method": getattr(fresh, "resolution_method", "")}
                                    ] + [c for c in fresh_candidates
                                         if c.get("product_id") != fresh.product_id]
            if not fresh_candidates:
                continue
            old_ids = [c.get("product_id") for c in (entry.get("candidates") or [])
                       if isinstance(c, dict)]
            new_ids = [c.get("product_id") for c in fresh_candidates]
            if old_ids == new_ids:
                continue
            entry["candidates"] = fresh_candidates[:5]
            changed += 1
        if changed:
            try:
                self.pending_store.save(record)
            except Exception:
                logger.exception("review.refresh_persist_failed", case_id=case_id)
                return 0
            try:
                metrics.incr("review_candidates_refreshed_total", lines=str(changed))
            except Exception:
                pass
            logger.info("review.candidates_refreshed", case_id=case_id, lines=changed)
        return changed

    def cancel_case(self, case_id: str, actor: str) -> dict[str, Any]:
        ctx = self.load_case(case_id)
        if ctx is None:
            raise CaseNotFound(case_id)
        record = ctx["record"]
        if record is not None and self.pending_store is not None:
            self.pending_store.delete(record.get("order_id", ""))
        # Mark the job terminal-cancelled so stale buttons render CANCELLED
        # instead of resurrecting actions for a deleted pending record.
        try:
            job = ctx["job"]
            from order_parser.core.job import JobStatus

            job.status = JobStatus.FAILED
            job.review_required = False
            job.error_code = "USER_CANCELLED"
            job.error_message = f"cancelled by {actor}"
            self.job_store.save(job)
        except Exception:
            logger.exception("corrections.cancel_mark_failed", case_id=case_id)
        self._audit(ctx, actor, "case_cancelled", {})
        return {"case_id": case_id, "cancelled": True}

    # ------------------------------------------------------------ internals

    def _require_actionable(self, case_id: str) -> dict[str, Any]:
        ctx = self.load_case(case_id)
        if ctx is None:
            raise CaseNotFound(case_id)
        job = ctx["job"]
        status = getattr(job, "status", "")
        status_value = status.value if hasattr(status, "value") else str(status)
        if status_value == "COMPLETED":
            raise CaseNotActionable("already completed")
        if ctx["record"] is None:
            raise CaseNotActionable("no pending record")
        return ctx

    def _require_item(self, ctx: dict[str, Any], item_index: int | None) -> int:
        record = ctx["record"]
        try:
            count = len(ParsedOrder.model_validate(record["parsed_order"]).order.items)
        except Exception:
            raise InvalidCorrection("order unreadable")
        if item_index is None or item_index < 0 or item_index >= count:
            raise InvalidCorrection("item out of range")
        return int(item_index)

    def _candidate(self, ctx: dict[str, Any], validation: dict[str, Any],
                   kind: str, item_index: int | None, ref: int) -> dict[str, Any]:
        if kind == "customer":
            pool = [c for c in ((validation.get("customer") or {}).get("candidates") or [])
                    if isinstance(c, dict)]
        else:
            products = [p for p in (validation.get("products") or []) if isinstance(p, dict)]
            if item_index is None or item_index < 0 or item_index >= len(products):
                raise InvalidCorrection("item out of range")
            pool = [c for c in (products[item_index].get("candidates") or []) if isinstance(c, dict)]
        if ref < 0 or ref >= len(pool):
            raise InvalidCorrection("choice is no longer available")
        return pool[ref]

    @staticmethod
    def _parse_number(value: str, kind: str) -> float:
        import re

        cleaned = re.sub(r"[^\d.\-]", "", (value or "").replace(",", ""))
        try:
            number = float(cleaned)
        except ValueError:
            raise InvalidCorrection(f"{kind} must be numeric (example: 25)")
        if number <= 0:
            raise InvalidCorrection(f"{kind} must be positive")
        return number

    def _staged(self, ctx: dict[str, Any], field: str, item_index: int | None,
                corrected: str, actor: str,
                target: dict[str, Any] | None = None) -> dict[str, Any]:
        record = ctx["record"]
        try:
            parsed = ParsedOrder.model_validate(record["parsed_order"])
        except Exception:
            parsed = None
        original = ""
        if parsed is not None:
            if field == "customer":
                original = parsed.order.customer.name or ""
            elif field.startswith("item.") and item_index is not None:
                try:
                    key = field.split(".", 1)[1]
                    current = getattr(parsed.order.items[int(item_index)], key, None)
                    original = "" if current is None else str(current)
                except (IndexError, ValueError):
                    original = ""
        return {
            "field": field, "item_index": item_index,
            "original_value": original, "corrected_value": corrected,
            "actor": actor, "timestamp": _now_iso(), "source": "telegram",
            "reason": "user correction", "target": target or {},
        }

    def _odoo_reference_price(self, record: dict[str, Any], item_index: int) -> float | None:
        product_id = None
        summary = (record.get("resolution") or {}).get("items") or []
        if 0 <= item_index < len(summary) and isinstance(summary[item_index], dict):
            product_id = summary[item_index].get("product_id")
        if product_id is None:
            products = [p for p in ((record.get("validation") or {}).get("products") or [])
                        if isinstance(p, dict)]
            if 0 <= item_index < len(products):
                product_id = products[item_index].get("product_id")
        catalog = getattr(self.resolver, "catalog", None)
        if product_id is not None and catalog is not None:
            try:
                product = catalog.get(int(product_id))
                if product and product.get("list_price") is not None:
                    return float(product["list_price"])
            except Exception:
                logger.exception("corrections.reference_price_failed")
        return None

    def _reprocess(self, ctx: dict[str, Any], actor: str, patches: dict[str, Any],
                   staged: list[dict[str, Any]], summary: str) -> dict[str, Any]:
        record = ctx["record"]
        job = ctx["job"]
        parsed = ParsedOrder.model_validate(record["parsed_order"])
        order = parsed.order

        if "customer_name" in patches:
            order.customer.name = str(patches["customer_name"])
        for raw_index, fields in (patches.get("items") or {}).items():
            index = int(raw_index)
            if index < 0 or index >= len(order.items):
                raise InvalidCorrection("item out of range")
            for key, value in fields.items():
                setattr(order.items[index], key, value)
        corrections = list(record.get("corrections") or [])
        corrections.extend(staged)
        record["corrections"] = corrections

        # Park the pending record so the re-run cannot self-match as a
        # duplicate of its own content; restored below (updated) or on error.
        old_order_id = str(record.get("order_id") or "")
        parked = dict(record)
        self.pending_store.delete(old_order_id)
        try:
            raw = dict(record.get("raw") or {})
            raw["job_id"] = job.job_id
            raw["corrections"] = record.get("corrections") or []
            raw["overrides"] = record.get("overrides") or {}
            result = self.pipeline.process(job.source, job.input_type or "text", parsed, raw)
        except Exception:
            self.pending_store.save(parked)
            raise
        new_order_id = str(result.get("order_id") or old_order_id)
        result["job_id"] = job.job_id
        try:
            job.result = result
            job.customer_detected = str(result.get("customer") or "")
            job.items_detected = int(result.get("items") or 0)
            status = result.get("status")
            if status == "success":
                from order_parser.core.job import JobStatus

                job.status = JobStatus.COMPLETED
                job.review_required = False
            elif status in ("pending", "review"):
                from order_parser.core.job import JobStatus

                job.status = JobStatus.NEEDS_REVIEW
                job.review_required = True
            self.job_store.save(job)
        except Exception:
            logger.exception("corrections.job_update_failed", case_id=job.job_id)
        self._audit(ctx, actor, "correction_applied",
                    {"summary": summary, "new_order_id": new_order_id,
                     "correction": staged[-1] if staged else {}})
        return {"case_id": job.job_id, "summary": summary, "result": result,
                "status": self._fresh_status(self.load_case(job.job_id) or ctx)}

    def _fresh_status(self, ctx: dict[str, Any]):
        from order_parser.user_actions.case import build_case_status

        job = ctx["job"]
        record = ctx["record"]
        if record is not None and self.pending_store is not None:
            record = self.pending_store.get(str(record.get("order_id") or "")) or record
        result = dict(ctx.get("result") or {})
        if isinstance(job.result, dict):
            merged = dict(job.result)
            merged.update({k: v for k, v in result.items() if v is not None})
            result = merged
        return build_case_status(job, record, result,
                                 uom_options=self.uom_options(),
                                 tax_options=self.tax_options())

    def _audit(self, ctx: dict[str, Any], actor: str, action: str, detail: dict[str, Any]) -> None:
        try:
            write_audit_entry({
                "event": "user_action",
                "case_id": ctx["job"].job_id,
                "order_id": ctx.get("order_id"),
                "actor": actor,
                "action": action,
                "detail": detail,
                "timestamp": _now_iso(),
            })
        except Exception:
            logger.exception("corrections.audit_failed", case_id=ctx["job"].job_id)
