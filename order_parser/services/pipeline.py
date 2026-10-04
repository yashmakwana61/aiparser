from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from order_parser.config import get_settings
from order_parser.core import metrics
from order_parser.core.audit import write_audit_entry
from order_parser.core.idempotency_store import IdempotencyStore
from order_parser.core.pending_store import PendingStore
from order_parser.integrations.odoo_client import OdooClient
from order_parser.resolution.normalization import matches_never_customer
from order_parser.models import ParsedOrder
from order_parser.resolution.duplicate_detector import fingerprint_order
from order_parser.resolution.models import (
    FUZZY_MATCH,
    PRODUCT_AMBIGUOUS,
    PRODUCT_UNRESOLVED,
    QUANTITY_CONFLICT,
    CUSTOMER_UNRESOLVED,
    CUSTOMER_AMBIGUOUS,
    DUPLICATE_ORDER,
    RESOLUTION_FAILED,
    EXACT_NAME,
    ResolutionStatus,
    ResolvedOrder,
    CustomerResolution,
)
from order_parser.resolution.order_resolver import OrderResolver
from order_parser.validators.customer_validator import CustomerValidator
from order_parser.validators.product_validator import ProductValidator

logger = structlog.get_logger(__name__)


class DuplicateCreationAttempt(Exception):
    """Raised when an idempotency claim shows another creation in flight."""


class OrderPipeline:
    """Orchestrates master data resolution, validation, the confidence
    decision engine, Odoo execution and audit logging for every ingested
    order.

    When an :class:`OrderResolver` is injected (production wiring), Odoo is the
    authoritative source for product/customer identity, UOM, price and tax;
    deterministic blocking issues gate the decision engine regardless of AI
    confidence. Without a resolver the pipeline behaves exactly as before.

    With persistent idempotency enabled (``enable_idempotency``), every
    successful ingestion is recorded in a local SQLite history and creation is
    guarded by an atomic claim: re-sent orders are routed to review instead of
    being created twice - across restarts and concurrent runs.
    """

    def __init__(
        self,
        odoo: OdooClient | None = None,
        settings=None,
        resolver: OrderResolver | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self.odoo = odoo or OdooClient()
        self.settings = settings or get_settings()
        self.resolver = resolver
        self.product_validator = ProductValidator(self.odoo, auto_create=self.settings.auto_create_products)
        self.customer_validator = CustomerValidator(self.odoo)
        self.pending_store = PendingStore()
        if resolver is not None and getattr(resolver, "pending_store", None) is None:
            resolver.pending_store = self.pending_store
            resolver.duplicates.pending_store = self.pending_store
        self.idempotency = idempotency_store
        if self.idempotency is None and bool(getattr(self.settings, "enable_idempotency", False)):
            path = getattr(self.settings, "idempotency_db_path", "") or str(
                Path(self.settings.log_dir) / "idempotency.sqlite3"
            )
            self.idempotency = IdempotencyStore(path)

    # ------------------------------------------------------------------ public

    def process(self, source: str, input_type: str, parsed: ParsedOrder, raw: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = raw or {}
        started = metrics.monotonic()

        def _tagged(result: dict[str, Any]) -> dict[str, Any]:
            metrics.incr("orders_processed_total", source=source, status=str(result.get("status")))
            metrics.observe("orders_processing_seconds", metrics.monotonic() - started)
            return result

        order_id = uuid.uuid4().hex[:12]
        parsed.order.metadata.source = source
        parsed.order.metadata.input_type = input_type
        try:
            resolved: ResolvedOrder | None = None
            if self.resolver is not None:
                try:
                    resolved = self.resolver.resolve(parsed)
                except Exception:
                    logger.exception("pipeline.resolve_failed", order_id=order_id, source=source)
                    resolved = OrderResolver.failed(parsed.order)

            if (
                resolved is not None
                and self.settings.auto_create_customers
                and self.settings.auto_create_all_orders
                and resolved.customer.status == ResolutionStatus.UNRESOLVED
            ):
                self._auto_create_customer(resolved, parsed, order_id, source)

            validation = self._validate(parsed.order, resolved)
            decision = self.decide(parsed.order.metadata.confidence, validation["is_valid"], resolved)
            if input_type == "image":
                products_valid = all(r.get("valid") for r in validation.get("products", []))
                decision = "pending" if products_valid else "review"
            if self.settings.auto_create_all_orders and validation["is_valid"] and (
                resolved is None
                or resolved.is_auto_eligible
                or resolved.is_auto_eligible_including_high_confidence_fuzzy
            ):
                decision = "auto"
            ai_meta = getattr(parsed, "ai_response", None)
            if isinstance(ai_meta, dict) and ai_meta.get("ocr_failed"):
                # Google Vision / interpretation failed: never create an order,
                # regardless of confidence or AUTO_CREATE_ALL_ORDERS.
                decision = "review"

            fingerprint = ""
            duplicate_of: str | None = None
            if self.idempotency is not None:
                try:
                    fingerprint = (
                        getattr(resolved, "fingerprint", "") if resolved is not None else fingerprint_order(parsed.order)
                    )
                except Exception:
                    logger.exception("pipeline.fingerprint_failed", order_id=order_id)
                    fingerprint = ""
                if fingerprint:
                    recent = self.idempotency.find_recent(
                        fingerprint, int(getattr(self.settings, "duplicate_window_hours", 24))
                    )
                    if recent and recent.get("status") == "success":
                        # Re-sent order that was already ingested recently:
                        # never create it twice; staff decides what to do.
                        duplicate_of = str(recent.get("order_ref") or "a recent ingestion")
                        decision = "review"
                        metrics.incr("duplicates_blocked_total", source=source)
                        logger.info(
                            "pipeline.duplicate_blocked",
                            order_id=order_id,
                            duplicate_of=duplicate_of,
                        )

            self._apply_resolution_to_items(parsed.order, resolved)
            result = self._execute(
                decision,
                order_id,
                source,
                parsed,
                validation,
                raw,
                resolved,
                idem={"fingerprint": fingerprint} if fingerprint else None,
                duplicate_of=duplicate_of,
            )
            self._audit(order_id, source, input_type, raw, parsed, validation, result, resolved)
            return _tagged(result)
        except Exception as exc:
            logger.exception("pipeline.process_failed", order_id=order_id, source=source)
            result = {"status": "error", "order_id": order_id, "message": str(exc), "source": source}
            self._audit(order_id, source, input_type, raw, parsed, {"is_valid": False}, result)
            return _tagged(result)

    def confirm_order(self, order_id: str, actor: str = "api") -> dict[str, Any]:
        record = self.pending_store.get(order_id)
        if not record:
            return {"status": "error", "order_id": order_id, "message": "order not found or already processed"}
        try:
            parsed = ParsedOrder.model_validate(record["parsed_order"])
        except Exception as exc:
            return {"status": "error", "order_id": order_id, "message": f"unreadable pending record: {exc}"}
        # Duplicate check takes precedence over the status guard: a queued
        # order whose content was meanwhile ingested must not be created.
        fingerprint = str(record.get("fingerprint") or "")
        if not fingerprint and self.idempotency is not None:
            try:
                fingerprint = fingerprint_order(parsed.order)
            except Exception:
                logger.exception("pipeline.fingerprint_failed", order_id=order_id)
                fingerprint = ""
        if self.idempotency is not None and fingerprint:
            recent = self.idempotency.find_recent(
                fingerprint, int(getattr(self.settings, "duplicate_window_hours", 24))
            )
            if recent and recent.get("status") == "success":
                return {
                    "status": "error",
                    "order_id": order_id,
                    "message": f"duplicate of recently ingested order {recent.get('order_ref') or '(unknown ref)'}",
                }
        if record.get("status") != "pending":
            return {"status": "error", "order_id": order_id, "message": "order requires manual review and cannot be auto-confirmed"}
        claimed = False
        try:
            validation = record.get("validation", {})
            if self.idempotency is not None and fingerprint:
                if not self.idempotency.claim(fingerprint, owner=order_id):
                    return {"status": "error", "order_id": order_id, "message": "another confirmation is in progress"}
                claimed = True
            try:
                res_summary = record.get("resolution") or {}
                missing_info = res_summary.get("missing_information") or []
                resolved_ns = type("ResolvedSnapshot", (), {"missing_information": missing_info})()
                created = self._create_sales_order(
                    parsed.order,
                    validation,
                    # ERP-side duplicate visibility only when the local
                    # history layer is active; legacy callers unchanged.
                    client_order_ref=fingerprint[:16] if (self.idempotency is not None and fingerprint) else "",
                    resolved=resolved_ns,
                    raw=record.get("raw"),
                )
            except Exception:
                if claimed and self.idempotency is not None:
                    self.idempotency.release(fingerprint)
                raise
            if claimed and self.idempotency is not None:
                self.idempotency.release(fingerprint)
                self.idempotency.record(
                    fingerprint,
                    status="success",
                    order_ref=created["name"],
                    source=parsed.order.metadata.source,
                )
            self.pending_store.delete(order_id)
            result = {
                "status": "success",
                "order_id": order_id,
                "sales_order": created["name"],
                "customer": parsed.order.customer.name,
                "items": len(parsed.order.items),
                "confidence": parsed.order.metadata.confidence,
                "actor": actor,
            }
            metrics.incr("orders_confirmed_total")
            self._audit(
                order_id,
                parsed.order.metadata.source,
                parsed.order.metadata.input_type,
                record.get("raw", {}),
                parsed,
                validation,
                result,
            )
            return result
        except Exception as exc:
            metrics.incr("orders_confirm_errors_total")
            logger.exception("pipeline.confirm_failed", order_id=order_id)
            return {"status": "error", "order_id": order_id, "message": str(exc)}

    def reject_order(self, order_id: str, actor: str = "api") -> dict[str, Any]:
        if not self.pending_store.get(order_id):
            return {"status": "error", "order_id": order_id, "message": "order not found"}
        self.pending_store.delete(order_id)
        metrics.incr("orders_rejected_total")
        return {"status": "rejected", "order_id": order_id, "actor": actor}

    def list_orders(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.pending_store.list(status=status)

    @staticmethod
    def _confidence_percent(confidence: float) -> float:
        """Normalize AI confidence to a 0-100 percentage.

        Models may return a 0-1 fraction (e.g. 0.98) or already 0-100 (e.g. 98).
        """
        confidence = float(confidence)
        return confidence * 100 if confidence <= 1.0 else confidence

    def decide(self, confidence: float, is_valid: bool, resolved: ResolvedOrder | None = None) -> str:
        """Confidence-based decision engine with deterministic safety gating.

        Legacy rules (no resolution available):
        - valid and confidence >= auto_create_threshold  -> "auto"  (create Sales Order)
        - valid and confidence >= confirm_threshold      -> "pending" (ask for confirmation)
        - anything else                                  -> "review" (manual review)

        With a ResolvedOrder attached, any deterministic blocking issue
        (ambiguous/unresolved product or customer, missing price/UOM, tax
        conflict, quantity conflict, duplicate) forces "review"; fuzzy-only
        matches cap the effective confidence below the auto-create band so
        they can land at most in the confirmation queue. High AI confidence
        never overrides these gates.
        """
        confidence = self._confidence_percent(confidence)
        if not is_valid:
            return "review"
        if resolved is not None:
            if resolved.blocking_issues:
                return "review"
            fuzzy_confidences = [
                item.product.confidence
                for item in resolved.items
                if item.product.resolution_method == FUZZY_MATCH and item.product.confidence is not None
            ]
            if resolved.customer.resolution_method == FUZZY_MATCH and resolved.customer.confidence is not None:
                fuzzy_confidences.append(resolved.customer.confidence)
            if fuzzy_confidences:
                confidence = min(confidence, min(fuzzy_confidences))
        if confidence >= float(self.settings.auto_create_threshold):
            if resolved is None or resolved.is_auto_eligible:
                return "auto"
            return "pending"
        if confidence >= float(self.settings.confirm_threshold):
            return "pending"
        return "review"

    # ----------------------------------------------------------------- internals

    def _validate(self, order, resolved: ResolvedOrder | None = None) -> dict[str, Any]:
        if resolved is None:
            product_results = self.product_validator.validate(order.items)
            customer_result = self.customer_validator.validate(order.customer)
            is_valid = all(r.get("valid") for r in product_results) and bool(customer_result.get("valid"))
            return {"is_valid": is_valid, "products": product_results, "customer": customer_result}

        products: list[dict[str, Any]] = []
        for item in resolved.items:
            entry: dict[str, Any] = {"product_name": item.item.product_name}
            if item.product.status == ResolutionStatus.RESOLVED:
                entry.update(
                    {
                        "valid": True,
                        "product_id": item.product.product_id,
                        "matched_name": item.product.product_name,
                        "score": item.product.confidence,
                        "method": item.product.resolution_method,
                    }
                )
                if item.product.resolution_method == FUZZY_MATCH:
                    entry["fuzzy_only"] = True
            else:
                reason = item.product.reason or (
                    "ambiguous_product" if item.product.status == ResolutionStatus.AMBIGUOUS else "product_not_found"
                )
                entry.update({"valid": False, "reason": reason, "candidates": item.product.candidates})
            products.append(entry)

        customer_res = resolved.customer
        if customer_res.status == ResolutionStatus.RESOLVED:
            customer_entry: dict[str, Any] = {
                "valid": True,
                "exists": True,
                "partner_id": customer_res.partner_id,
                "partner_name": customer_res.partner_name,
                "method": customer_res.resolution_method,
            }
        else:
            customer_entry = {
                "valid": False,
                "reason": customer_res.reason or ("ambiguous_customer" if customer_res.status == ResolutionStatus.AMBIGUOUS else "customer_unresolved"),
                "candidates": customer_res.candidates,
            }

        is_valid = all(p.get("valid") for p in products) and bool(customer_entry.get("valid"))
        return {"is_valid": is_valid, "products": products, "customer": customer_entry}

    # --------------------------------------------------------------- auto-create customer

    def _auto_create_customer(
        self, resolved: ResolvedOrder, parsed: ParsedOrder, order_id: str, source: str
    ) -> None:
        """Create a new Odoo partner when the customer could not be matched.

        Patches the ResolvedOrder in-place so the downstream decision engine
        sees a valid, deterministic customer and can proceed with auto-creation.
        Known order collectors/vendors (never_customer_names) are never
        created — that would corrupt the customer master.
        """
        name = (parsed.order.customer.name or "").strip()
        if name and matches_never_customer(
            name, getattr(self.settings, "never_customer_names", "")
        ):
            logger.warning("pipeline.auto_create_customer_collector_skipped", order_id=order_id, name=name)
            return
        customer_model = parsed.order.customer
        if not name:
            logger.warning("pipeline.auto_create_customer_no_name", order_id=order_id)
            return

        try:
            partner_id = self.odoo.create_partner(customer_model)
        except Exception:
            logger.exception("pipeline.auto_create_customer_failed", order_id=order_id, name=name)
            return

        logger.info(
            "pipeline.auto_customer_created",
            order_id=order_id,
            partner_id=partner_id,
            name=name,
            source=source,
        )

        # Patch the resolved customer so downstream sees it as resolved.
        resolved.customer = CustomerResolution(
            status=ResolutionStatus.RESOLVED,
            source="auto_created",
            resolution_method=EXACT_NAME,
            value=name,
            confidence=100.0,
            reference_id=partner_id,
            partner_id=partner_id,
            partner_name=name,
        )

        # Remove the CUSTOMER_UNRESOLVED / CUSTOMER_AMBIGUOUS blocking issues
        # that the resolver placed.
        resolved.blocking_issues = [
            bi for bi in resolved.blocking_issues
            if bi.code not in (CUSTOMER_UNRESOLVED, CUSTOMER_AMBIGUOUS)
        ]

    @staticmethod
    def _apply_resolution_to_items(order, resolved: ResolvedOrder | None) -> None:
        if resolved is None:
            return
        for item, resolved_item in zip(order.items, resolved.items):
            if resolved_item.product.status == ResolutionStatus.RESOLVED and resolved_item.product.product_id:
                item.product_id = resolved_item.product.product_id
            if item.unit_price is None and resolved_item.price.unit_price is not None:
                item.unit_price = resolved_item.price.unit_price
            if item.uom is None and resolved_item.uom.uom_name is not None:
                item.uom = resolved_item.uom.uom_name
            try:
                if abs(float(resolved_item.quantity_effective) - float(item.quantity or 0)) > 1e-9:
                    item.quantity = resolved_item.quantity_effective
            except (TypeError, ValueError):
                continue

    # ---------------------------------------------------------- Phase 18: readiness

    @staticmethod
    def _determine_readiness_status(
        parsed: ParsedOrder,
        resolved: ResolvedOrder | None,
    ) -> str:
        """Determine the Phase 18 readiness status from extracted + resolved data.

        Returns one of: READY_FOR_ODOO, MISSING_CUSTOMER, MISSING_ORDER_DETAILS,
        PRODUCT_AMBIGUOUS, PRODUCT_UNKNOWN, INVALID_QUANTITY, PARSER_FAILURE,
        ODOO_UNAVAILABLE.
        """
        if resolved is None:
            # Resolution failed entirely — fall back to validity check.
            if not parsed.order.customer.name.strip():
                return "MISSING_CUSTOMER"
            if not parsed.order.items:
                return "MISSING_ORDER_DETAILS"
            return "PARSER_FAILURE"

        # Detect Odoo-wide unavailability: if the customer resolver returned
        # "odoo_unavailable" or "catalog_unavailable", the integration is
        # down — not a data problem.
        cust_reason = resolved.customer.reason or ""
        if cust_reason == "odoo_unavailable":
            return "ODOO_UNAVAILABLE"
        product_reasons = [
            (item.product.reason or "") for item in resolved.items
        ]
        if all(r == "catalog_unavailable" for r in product_reasons) and product_reasons:
            return "ODOO_UNAVAILABLE"

        # Customer must be resolved to create any SO.
        if resolved.customer.status != ResolutionStatus.RESOLVED:
            return "MISSING_CUSTOMER"

        # At least one usable order line is required.
        has_usable_line = False
        for item in resolved.items:
            qty = 0.0
            try:
                qty = float(item.item.quantity or 0)
            except (TypeError, ValueError):
                pass
            if qty <= 0:
                continue
            if item.product.status == ResolutionStatus.RESOLVED and item.product.product_id:
                has_usable_line = True
                break
        if not has_usable_line:
            # Distinguish: is it because no product resolved, or no quantity?
            any_product_resolved = any(
                i.product.status == ResolutionStatus.RESOLVED and i.product.product_id
                for i in resolved.items
            )
            any_positive_qty = any(
                float(i.item.quantity or 0) > 0 for i in resolved.items
            )
            if any_positive_qty and not any_product_resolved:
                has_any_product_ambiguity = any(
                    i.product.status == ResolutionStatus.AMBIGUOUS for i in resolved.items
                )
                if has_any_product_ambiguity:
                    return "PRODUCT_AMBIGUOUS"
                return "PRODUCT_UNKNOWN"
            if not any_positive_qty and any_product_resolved:
                return "INVALID_QUANTITY"
            if not any_positive_qty and not any_product_resolved:
                return "MISSING_ORDER_DETAILS"
            return "MISSING_ORDER_DETAILS"

        return "READY_FOR_ODOO"

    def _build_readiness(
        self,
        order_id: str,
        source: str,
        parsed: ParsedOrder,
        resolved: ResolvedOrder | None,
    ) -> dict[str, Any]:
        """Build the Phase 18 readiness payload for inclusion in every result."""
        readiness_status = self._determine_readiness_status(parsed, resolved)

        customer_payload = None
        if resolved is not None:
            cust = resolved.customer
            # Collector-rerouted orders show the effective (deliver-to)
            # customer, not the vendor name from the customer slot.
            effective_name = (cust.details or {}).get("customer_name_effective")
            customer_payload = {
                "raw_name": effective_name or parsed.order.customer.name,
                "resolved": cust.status == ResolutionStatus.RESOLVED,
                "partner_id": cust.partner_id,
                "partner_name": cust.partner_name,
            }
        else:
            customer_payload = {
                "raw_name": parsed.order.customer.name,
                "resolved": False,
                "partner_id": None,
                "partner_name": None,
            }

        item_payloads: list[dict[str, Any]] = []
        if resolved is not None:
            for ri in resolved.items:
                item_payloads.append({
                    "raw_name": ri.item.product_name,
                    "product_id": ri.product.product_id,
                    "product_name": ri.product.product_name,
                    "quantity": float(ri.item.quantity or 0),
                    "uom": ri.uom.uom_name,
                    "price": ri.price.unit_price,
                    "tax_ids": list(ri.tax.tax_ids),
                    "missing_fields": list(ri.missing_fields),
                })
        else:
            for item in parsed.order.items:
                missing = []
                if not item.product_id:
                    missing.append("product")
                if not item.unit_price:
                    missing.append("price")
                item_payloads.append({
                    "raw_name": item.product_name,
                    "product_id": item.product_id,
                    "product_name": item.product_name,
                    "quantity": float(item.quantity or 0),
                    "uom": item.uom,
                    "price": item.unit_price,
                    "tax_ids": [],
                    "missing_fields": missing,
                })

        missing_info = resolved.missing_information if resolved is not None else []
        warning_codes = [w.code for w in resolved.warnings] if resolved is not None else []
        blocking_codes = [i.code for i in resolved.blocking_issues] if resolved is not None else []

        blocking_odoo = readiness_status not in ("READY_FOR_ODOO",)
        blocking_tally = bool(missing_info)

        return {
            "readiness_status": readiness_status,
            "customer_detail": customer_payload,
            "items_detail_readiness": item_payloads,
            "missing_information": missing_info,
            "warnings": warning_codes,
            "blocking_for_odoo": blocking_odoo,
            "blocking_for_tally": blocking_tally,
        }

    def _execute(
        self,
        decision: str,
        order_id: str,
        source: str,
        parsed: ParsedOrder,
        validation: dict[str, Any],
        raw: dict[str, Any],
        resolved: ResolvedOrder | None = None,
        idem: dict[str, Any] | None = None,
        duplicate_of: str | None = None,
    ) -> dict[str, Any]:
        order = parsed.order
        base = {
            "order_id": order_id,
            "customer": order.customer.name,
            "items": len(order.items),
            "confidence": self._confidence_percent(order.metadata.confidence),
            "source": source,
            "items_detail": [item.model_dump() for item in order.items],
        }
        if duplicate_of:
            base["duplicate_of"] = duplicate_of
        if resolved is not None:
            base["resolution_warnings"] = [w.code for w in resolved.warnings]
            base["resolution_blocked"] = [i.code for i in resolved.blocking_issues]

        # Phase 18: readiness response fields appended to every result.
        readiness = self._build_readiness(order_id, source, parsed, resolved)
        base.update(readiness)

        if decision == "auto":
            fingerprint = (idem or {}).get("fingerprint", "")
            try:
                created = self._guarded_create(order, validation, fingerprint, order_id, source, resolved=resolved, raw=raw)
            except DuplicateCreationAttempt:
                metrics.incr("claim_conflicts_total")
                logger.warning("pipeline.claim_conflict", order_id=order_id)
                return {
                    **base,
                    "status": "review",
                    "mode": "auto",
                    "message": "Duplicate submission detected while creating; sent for review.",
                    "duplicate_of": "in-flight creation",
                }
            return {**base, "status": "success", "sales_order": created["name"], "mode": "auto"}

        record: dict[str, Any] = {
            "order_id": order_id,
            "status": "pending" if decision == "pending" else "review",
            "source": source,
            "input_type": order.metadata.input_type,
            "parsed_order": parsed.model_dump(),
            "validation": validation,
            "raw": raw,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        record_fingerprint = (idem or {}).get("fingerprint", "")
        if resolved is not None:
            record["resolution"] = resolved.summary()
            record["fingerprint"] = resolved.fingerprint
        elif record_fingerprint:
            record["fingerprint"] = record_fingerprint
        self.pending_store.save(record)
        if decision == "pending":
            return {
                **base,
                "status": "pending",
                "message": f"Order {order_id} awaits confirmation (confidence {self._confidence_percent(order.metadata.confidence):.0f}%). Reply CONFIRM {order_id} to proceed.",
            }
        if duplicate_of:
            message = f"Duplicate of {duplicate_of}; order sent for manual review instead of being created twice."
        else:
            message = "Order sent for manual review."
        return {**base, "status": "review", "message": message}

    def _guarded_create(self, order, validation: dict[str, Any], fingerprint: str, order_id: str, source: str, resolved=None, raw=None) -> dict[str, Any]:
        """Create the sales order under an idempotency claim when enabled.

        Without a store (feature disabled) this is exactly the legacy create.
        With one, a concurrent/retried run for the same content is routed to
        review instead of double-creating; failures release the claim so staff
        can retry immediately, and success is recorded in history.
        """
        if not (self.idempotency and fingerprint):
            return self._create_sales_order(order, validation, resolved=resolved, raw=raw)

        if not self.idempotency.claim(fingerprint, owner=order_id):
            raise DuplicateCreationAttempt(fingerprint)
        try:
            created = self._create_sales_order(order, validation, client_order_ref=fingerprint[:16], resolved=resolved, raw=raw)
        except Exception:
            self.idempotency.release(fingerprint)
            raise
        self.idempotency.release(fingerprint)
        self.idempotency.record(fingerprint, status="success", order_ref=created.get("name", ""), source=source)
        return created

    def _create_sales_order(self, order, validation: dict[str, Any], client_order_ref: str = "", resolved=None, raw=None) -> dict[str, Any]:
        partner_id = validation.get("customer", {}).get("partner_id")
        if not partner_id:
            if self.resolver is not None:
                raise ValueError("customer_not_resolved_refusing_to_create_partner")
            partner_id = self.odoo.create_partner(order.customer)
        for item, product_result in zip(order.items, validation.get("products", [])):
            item.product_id = item.product_id or product_result.get("product_id")
            if item.unit_price is None:
                item.unit_price = product_result.get("price")

        notes = order.metadata.notes or ""
        missing = getattr(resolved, "missing_information", None) or []
        if missing:
            missing_line = f"[AI Parser] Missing fields: {', '.join(missing)} \u2014 Odoo defaults will apply"
            notes = f"{notes}\n{missing_line}" if notes else missing_line

        kwargs: dict[str, Any] = {"notes": notes}
        if client_order_ref:
            kwargs["client_order_ref"] = client_order_ref
        created = self.odoo.create_sale_order(partner_id, order.items, **kwargs)

        if raw and created.get("id"):
            chat_id = str(raw.get("chat_id", ""))
            sender = raw.get("sender", "")
            session_id = str(raw.get("session_id", ""))
            missing = getattr(resolved, "missing_information", None) or []

            item_lines = []
            for it in order.items:
                item_lines.append(f"{it.product_name} x{it.quantity} ({it.uom}) @ {it.unit_price or '?'}")
            order_summary = f"Order {created['name']} for {order.customer.name}:\n" + "\n".join(item_lines)
            if missing:
                order_summary += f"\n\nMissing fields: {', '.join(missing)}"

            error_msg = ""
            if missing:
                error_msg = f"Missing fields: {', '.join(missing)} — Odoo defaults will apply"

            raw_text = raw.get("text", "")

            messages = raw.get("messages") or []
            if not messages and raw_text:
                messages = [{"text": raw_text}]
            for msg in messages:
                msg_text = msg.get("text", "")
                if msg_text:
                    self.odoo.create_telegram_message(
                        chat_id, sender, msg_text, created["id"],
                        error_message=error_msg,
                        raw_update=raw_text,
                        session_id=session_id,
                    )

            if not messages:
                self.odoo.create_telegram_message(
                    chat_id, sender, order_summary, created["id"],
                    error_message=error_msg,
                    raw_update=raw_text,
                    session_id=session_id,
                )

            file_data = raw.get("file_data")
            if file_data and isinstance(file_data, dict):
                fname = file_data.get("filename", "input")
                fbytes = file_data.get("data")
                if fname and fbytes:
                    self.odoo.upload_file_to_odoo(fname, fbytes, "sale.order", created["id"])

        return created

    def _audit(
        self,
        order_id: str,
        source: str,
        input_type: str,
        raw: dict[str, Any],
        parsed: ParsedOrder,
        validation: dict[str, Any],
        result: dict[str, Any],
        resolved: ResolvedOrder | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "order_id": order_id,
            "source": source,
            "input_type": input_type,
            "original_message": raw.get("text")
            or raw.get("subject")
            or raw.get("filename")
            or raw.get("sender")
            or "",
            "extracted_text": getattr(parsed, "extracted_text", "")[:20000],
            "ai_response": getattr(parsed, "ai_response", {}),
            "normalized_json": parsed.order.model_dump(),
            "validation_result": validation,
            "sales_order": result.get("sales_order"),
            "status": result.get("status"),
            "confidence": parsed.order.metadata.confidence,
        }
        if resolved is not None:
            entry["resolution"] = resolved.summary()
        write_audit_entry(entry)
