from __future__ import annotations

import structlog

from order_parser.config import get_settings
from order_parser.models import ItemModel
from order_parser.resolution.models import (
    APPROVED_FALLBACK,
    CUSTOMER_PRICELIST,
    EXPLICIT_ORDER_PRICE,
    PRODUCT_SALES_PRICE,
    PriceResolution,
    ResolutionStatus,
)

logger = structlog.get_logger(__name__)


class PriceResolver:
    """Price resolution; never hallucinates a price.

    Hierarchy: explicit order price (accepted, tagged source='order', audited)
    -> customer-specific Odoo pricelist -> Odoo product sales price ->
    config-enabled approved fallback -> exception. When an explicit price
    deviates from the Odoo-derived reference beyond the configured tolerance,
    a deviation record is attached in ``details`` for the caller to surface
    as a warning.
    """

    def __init__(self, odoo, catalog=None, settings=None):
        self.odoo = odoo
        self.catalog = catalog
        self.settings = settings or get_settings()
        self.deviation_tolerance_pct = float(self.settings.price_deviation_tolerance_pct)

    def resolve(self, item: ItemModel, product_resolution, partner_id: int | None) -> PriceResolution:
        resolution = PriceResolution()

        odoo_reference = self._odoo_reference_price(item, product_resolution, partner_id)

        # Level 1: explicit order price - accepted and tagged per policy.
        if item.unit_price is not None:
            resolution.status = ResolutionStatus.RESOLVED
            resolution.source = "order"
            resolution.resolution_method = EXPLICIT_ORDER_PRICE
            resolution.unit_price = float(item.unit_price)
            resolution.value = float(item.unit_price)
            if odoo_reference is not None:
                resolution.details["odoo_reference_price"] = odoo_reference
                deviation = self._deviation_pct(float(item.unit_price), odoo_reference)
                if deviation is not None and deviation > self.deviation_tolerance_pct:
                    resolution.details["deviation_pct"] = round(deviation, 2)
            return resolution

        product_id = product_resolution.product_id if product_resolution else None
        quantity = float(item.quantity or 0) or 1.0

        # Level 2: customer-specific pricelist.
        if partner_id and product_id:
            pricelist_id = self._get_pricelist(partner_id)
            if pricelist_id:
                price = self._compute_pricelist_price(pricelist_id, product_id, quantity, partner_id)
                if price is not None:
                    resolution.status = ResolutionStatus.RESOLVED
                    resolution.source = "odoo"
                    resolution.resolution_method = CUSTOMER_PRICELIST
                    resolution.unit_price = float(price)
                    resolution.value = float(price)
                    resolution.pricelist_id = pricelist_id
                    resolution.reference_id = pricelist_id
                    return resolution

        # Level 3: Odoo product sales price.
        list_price = self._list_price(product_id)
        if list_price is not None:
            resolution.status = ResolutionStatus.RESOLVED
            resolution.source = "odoo"
            resolution.resolution_method = PRODUCT_SALES_PRICE
            resolution.unit_price = float(list_price)
            resolution.value = float(list_price)
            resolution.reference_id = product_id
            return resolution

        # Level 4: approved fallback (disabled unless explicitly enabled).
        if bool(getattr(self.settings, "approved_price_fallback", False)):
            resolution.status = ResolutionStatus.RESOLVED
            resolution.source = "config"
            resolution.resolution_method = APPROVED_FALLBACK
            resolution.unit_price = 0.0
            resolution.value = 0.0
            resolution.reason = "approved_fallback_zero_price"
            return resolution

        # Level 5: exception - price mandatory and nothing deterministic found.
        resolution.status = ResolutionStatus.UNRESOLVED
        resolution.reason = "price_missing"
        return resolution

    def _odoo_reference_price(self, item, product_resolution, partner_id) -> float | None:
        """Best Odoo-side unit price; used both as fallback source and deviation check."""
        product_id = product_resolution.product_id if product_resolution else None
        if not product_id:
            return None
        quantity = float(item.quantity or 0) or 1.0
        if partner_id:
            pricelist_id = self._get_pricelist(partner_id)
            if pricelist_id:
                price = self._compute_pricelist_price(pricelist_id, product_id, quantity, partner_id)
                if price is not None:
                    return float(price)
        return self._list_price(product_id)

    def _list_price(self, product_id) -> float | None:
        if not product_id or self.catalog is None:
            return None
        try:
            product = self.catalog.get(product_id)
        except Exception:
            logger.exception("price.catalog_lookup_failed", product_id=product_id)
            return None
        if product and product.get("list_price") is not None:
            try:
                return float(product["list_price"])
            except (TypeError, ValueError):
                return None
        return None

    def _get_pricelist(self, partner_id: int) -> int | None:
        try:
            return self.odoo.get_partner_pricelist(partner_id)
        except Exception:
            logger.exception("price.pricelist_lookup_failed", partner_id=partner_id)
            return None

    def _compute_pricelist_price(self, pricelist_id, product_id, quantity, partner_id):
        try:
            return self.odoo.compute_pricelist_price(pricelist_id, product_id, quantity, partner_id)
        except Exception:
            logger.exception("price.pricelist_compute_failed", pricelist_id=pricelist_id)
            return None

    @staticmethod
    def _deviation_pct(explicit: float, reference: float) -> float | None:
        if not reference:
            return None
        return abs(explicit - reference) / reference * 100.0
