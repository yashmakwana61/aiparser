from __future__ import annotations

from pathlib import Path

import structlog

from order_parser.config import get_settings
from order_parser.models import ParsedOrder
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.customer_resolver import CustomerResolver
from order_parser.resolution.duplicate_detector import DuplicateDetector, fingerprint_order
from order_parser.resolution.models import (
    APPROVED_FALLBACK,
    CUSTOMER_AMBIGUOUS,
    CUSTOMER_UNRESOLVED,
    DUPLICATE_ORDER,
    PRICE_DEVIATION,
    PRICE_FALLBACK,
    PRICE_MISSING,
    PRODUCT_AMBIGUOUS,
    PRODUCT_UNRESOLVED,
    QUANTITY_CONFLICT,
    RESOLUTION_FAILED,
    TAX_CONFLICT,
    TAX_UNRESOLVED,
    UOM_UNRESOLVED,
    UOM_MISSING_WARNING,
    PRICE_MISSING_WARNING,
    TAX_MISSING_WARNING,
    BlockingIssue,
    CustomerResolution,
    PriceResolution,
    ProductResolution,
    ResolutionStatus,
    ResolvedItem,
    ResolvedOrder,
    TaxResolution,
    UOMResolution,
)
from order_parser.resolution.price_resolver import PriceResolver
from order_parser.resolution.product_resolver import ProductResolver
from order_parser.resolution.tax_resolver import TaxResolver
from order_parser.resolution.uom_resolver import UOMResolver

logger = structlog.get_logger(__name__)


class OrderResolver:
    """Master data resolution orchestrator.

    Turns an AI-parsed :class:`ParsedOrder` into a :class:`ResolvedOrder` in
    which every ERP-controlled value carries its deterministic provenance.
    The customer resolves first (customer-scoped aliases, pricelists and
    fiscal positions depend on it), then each item resolves product -> UOM ->
    price -> tax, followed by quantity and duplicate checks. All findings are
    aggregated into blocking issues and warnings; nothing here mutates Odoo.
    """

    def __init__(
        self,
        odoo,
        catalog: CatalogProvider | None = None,
        alias_store: AliasStore | None = None,
        pending_store=None,
        settings=None,
        conversions_path: str | Path | None = None,
    ):
        self.odoo = odoo
        self.settings = settings or get_settings()
        self.alias_store = alias_store or AliasStore()
        self.catalog = catalog or CatalogProvider(odoo)
        self.pending_store = pending_store

        self.products = ProductResolver(self.catalog, self.alias_store, self.settings)
        self.customers = CustomerResolver(odoo, self.alias_store, self.settings)
        self.uoms = UOMResolver(odoo, conversions_path, self.settings)
        self.prices = PriceResolver(odoo, self.catalog, self.settings)
        self.taxes = TaxResolver(odoo, self.settings)
        self.duplicates = DuplicateDetector(
            pending_store, int(self.settings.duplicate_window_hours)
        )

    def resolve(
        self,
        parsed: ParsedOrder,
        session_partner_id: int | None = None,
        staff_partner_id: int | None = None,
    ) -> ResolvedOrder:
        order = parsed.order
        warnings: list[BlockingIssue] = []
        blocking: list[BlockingIssue] = []

        ai_customer = {}
        ai_items: list[dict] = []
        try:
            if isinstance(parsed.ai_response, dict):
                candidate = parsed.ai_response.get("customer")
                ai_customer = candidate if isinstance(candidate, dict) else {}
                items_candidate = parsed.ai_response.get("items")
                ai_items = items_candidate if isinstance(items_candidate, list) else []
        except Exception:
            logger.exception("resolver.ai_context_unreadable")

        explicit_ref = ai_customer.get("id")
        try:
            customer_resolution = self.customers.resolve(
                order.customer,
                session_partner_id=session_partner_id,
                staff_partner_id=staff_partner_id,
                explicit_reference=explicit_ref,
            )
        except Exception:
            logger.exception("resolver.customer_failed")
            customer_resolution = CustomerResolution(status=ResolutionStatus.UNRESOLVED, reason="resolver_error")
        partner_id = customer_resolution.partner_id

        if customer_resolution.status == ResolutionStatus.AMBIGUOUS:
            blocking.append(BlockingIssue(code=CUSTOMER_AMBIGUOUS, message="Customer match is ambiguous"))
        elif customer_resolution.status == ResolutionStatus.UNRESOLVED:
            blocking.append(
                BlockingIssue(code=CUSTOMER_UNRESOLVED, message=f"Customer not resolved ({customer_resolution.reason})")
            )

        resolved_items: list[ResolvedItem] = []
        strict = bool(getattr(self.settings, "strict_resolution", False))
        for index, item in enumerate(order.items):
            raw_taxes = None
            if index < len(ai_items) and isinstance(ai_items[index], dict):
                raw_taxes = ai_items[index].get("taxes")

            product_resolution = self._safe(
                lambda: self.products.resolve(item.product_name, partner_id),
                ProductResolution(raw_name=item.product_name),
                "product",
            )
            uom_resolution, factor = self._safe(
                lambda: self.uoms.resolve(item.uom, product_resolution),
                (UOMResolution(parsed_uom=item.uom or ""), 1.0),
                "uom",
            )
            price_resolution = self._safe(
                lambda: self.prices.resolve(item, product_resolution, partner_id),
                PriceResolution(),
                "price",
            )
            tax_resolution = self._safe(
                lambda: self.taxes.resolve(raw_taxes, product_resolution, partner_id),
                TaxResolution(),
                "tax",
            )

            try:
                quantity_effective = float(item.quantity or 0) * float(factor)
            except (TypeError, ValueError):
                quantity_effective = 0.0

            missing_fields: list[str] = []
            if uom_resolution.status == ResolutionStatus.UNRESOLVED:
                missing_fields.append("uom")
            if price_resolution.status == ResolutionStatus.UNRESOLVED:
                missing_fields.append("price")
            if tax_resolution.status == ResolutionStatus.UNRESOLVED:
                missing_fields.append("tax")

            resolved_items.append(
                ResolvedItem(
                    index=index,
                    item=item,
                    quantity_effective=quantity_effective,
                    product=product_resolution,
                    uom=uom_resolution,
                    price=price_resolution,
                    tax=tax_resolution,
                    missing_fields=missing_fields,
                )
            )

            if float(item.quantity or 0) <= 0:
                blocking.append(
                    BlockingIssue(code=QUANTITY_CONFLICT, message="Quantity must be positive", item_index=index)
                )
            if product_resolution.status == ResolutionStatus.AMBIGUOUS:
                blocking.append(
                    BlockingIssue(code=PRODUCT_AMBIGUOUS, message=f"Ambiguous product {item.product_name!r}", item_index=index)
                )
            elif product_resolution.status == ResolutionStatus.UNRESOLVED:
                blocking.append(
                    BlockingIssue(code=PRODUCT_UNRESOLVED, message=f"Product {item.product_name!r} not resolved", item_index=index)
                )

            # Phase 18: UOM, price, and tax are non-blocking warnings by
            # default (Odoo applies its own defaults). Only when
            # strict_resolution is True do they remain creation-blocking.
            if uom_resolution.status == ResolutionStatus.UNRESOLVED:
                if strict:
                    blocking.append(
                        BlockingIssue(code=UOM_UNRESOLVED, message=f"UOM {item.uom!r} not resolved", item_index=index)
                    )
                else:
                    warnings.append(
                        BlockingIssue(code=UOM_MISSING_WARNING, message=f"UOM not provided for {item.product_name!r}", item_index=index)
                    )
            if price_resolution.status == ResolutionStatus.UNRESOLVED:
                if strict:
                    blocking.append(
                        BlockingIssue(code=PRICE_MISSING, message="No deterministic price available", item_index=index)
                    )
                else:
                    warnings.append(
                        BlockingIssue(code=PRICE_MISSING_WARNING, message=f"Price not available for {item.product_name!r}", item_index=index)
                    )
            if tax_resolution.reason == TAX_CONFLICT:
                blocking.append(
                    BlockingIssue(code=TAX_CONFLICT, message="Trusted explicit tax conflicts with Odoo configuration", item_index=index)
                )
            elif tax_resolution.status == ResolutionStatus.UNRESOLVED:
                if strict:
                    blocking.append(
                        BlockingIssue(code=TAX_UNRESOLVED, message="No tax configuration could be resolved", item_index=index)
                    )
                else:
                    warnings.append(
                        BlockingIssue(code=TAX_MISSING_WARNING, message=f"Tax not resolved for {item.product_name!r}", item_index=index)
                    )

            if price_resolution.details.get("deviation_pct") is not None:
                warnings.append(
                    BlockingIssue(
                        code=PRICE_DEVIATION,
                        message=(
                            f"Explicit price deviates {price_resolution.details['deviation_pct']}% "
                            f"from Odoo reference {price_resolution.details.get('odoo_reference_price')}"
                        ),
                        item_index=index,
                    )
                )
            if price_resolution.resolution_method == APPROVED_FALLBACK:
                warnings.append(
                    BlockingIssue(code=PRICE_FALLBACK, message="Approved zero-price fallback used", item_index=index)
                )

        fingerprint = fingerprint_order(order)
        try:
            duplicate_of = self.duplicates.find_duplicate(fingerprint) if self.pending_store else None
        except Exception:
            logger.exception("resolver.duplicate_check_failed")
            duplicate_of = None
        if duplicate_of:
            blocking.append(
                BlockingIssue(code=DUPLICATE_ORDER, message=f"Duplicate of recently ingested order {duplicate_of}")
            )

        return ResolvedOrder(
            order=order,
            customer=customer_resolution,
            items=resolved_items,
            warnings=warnings,
            blocking_issues=blocking,
            fingerprint=fingerprint,
        )

    @staticmethod
    def failed(order) -> ResolvedOrder:
        """Degenerate result used when resolution itself crashes upstream."""
        return ResolvedOrder(
            order=order,
            blocking_issues=[BlockingIssue(code=RESOLUTION_FAILED, message="Resolution layer failed; manual review required")],
        )

    def _safe(self, fn, fallback, label: str):
        try:
            return fn()
        except Exception:
            logger.exception("resolver.field_failed", field=label)
            return fallback
