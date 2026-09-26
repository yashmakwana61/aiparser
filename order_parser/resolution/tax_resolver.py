from __future__ import annotations

import structlog

from order_parser.config import get_settings
from order_parser.resolution.models import (
    COMPANY_DEFAULT,
    CUSTOMER_FISCAL_POSITION,
    EXPLICIT_TAX,
    PRODUCT_TAX,
    TAX_CONFLICT,
    TaxResolution,
    ResolutionStatus,
)

logger = structlog.get_logger(__name__)


class TaxResolver:
    """Tax resolution; never infers tax from AI knowledge.

    Hierarchy: explicit trusted tax (only while configured) -> Odoo product
    taxes -> customer fiscal position mapping -> company default sale taxes ->
    exception. A disagreement between trusted explicit taxes and the product
    configuration is reported as a conflict, not silently resolved.
    """

    def __init__(self, odoo, settings=None):
        self.odoo = odoo
        self.settings = settings or get_settings()

    def resolve(
        self,
        raw_tax_names: list | None,
        product_resolution,
        partner_id: int | None,
    ) -> TaxResolution:
        resolution = TaxResolution()
        raw_names = [str(t) for t in (raw_tax_names or []) if str(t).strip()]

        product_tax_ids: list[int] = []
        if product_resolution is not None:
            product_tax_ids = [int(t) for t in (product_resolution.details or {}).get("taxes_id") or []]

        # Level 1: explicit trusted tax.
        if raw_names and bool(getattr(self.settings, "trust_explicit_taxes", False)):
            resolution.explicit_tax_names = raw_names
            numeric_ids = self._as_ids(raw_names)
            if numeric_ids and product_tax_ids and set(numeric_ids) != set(product_tax_ids):
                return TaxResolution(
                    status=ResolutionStatus.UNRESOLVED,
                    source="order",
                    reason=TAX_CONFLICT,
                    explicit_tax_names=raw_names,
                    details={"explicit_tax_ids": numeric_ids, "product_tax_ids": product_tax_ids},
                )
            resolution.status = ResolutionStatus.RESOLVED
            resolution.source = "order"
            resolution.resolution_method = EXPLICIT_TAX
            resolution.value = raw_names
            resolution.tax_ids = numeric_ids
            if numeric_ids:
                resolution.reference_id = numeric_ids[0]
            return resolution

        # Conflict guard still applies when explicit taxes are present but untrusted.
        if raw_names and product_tax_ids:
            numeric_ids = self._as_ids(raw_names)
            if numeric_ids and set(numeric_ids) != set(product_tax_ids):
                return TaxResolution(
                    status=ResolutionStatus.UNRESOLVED,
                    source="odoo",
                    reason=TAX_CONFLICT,
                    explicit_tax_names=raw_names,
                    details={"explicit_tax_ids": numeric_ids, "product_tax_ids": product_tax_ids},
                )

        # Level 2: Odoo product taxes; an active fiscal position that remaps
        # them deterministically takes credit for the mapping.
        if product_tax_ids:
            if partner_id:
                fiscal_position_id = self._get_fiscal_position(partner_id)
                if fiscal_position_id:
                    mapped = self._map_taxes(fiscal_position_id, product_tax_ids)
                    if set(mapped) != set(product_tax_ids):
                        resolution.status = ResolutionStatus.RESOLVED
                        resolution.source = "odoo"
                        resolution.resolution_method = CUSTOMER_FISCAL_POSITION
                        resolution.value = mapped
                        resolution.tax_ids = mapped
                        resolution.fiscal_position_id = fiscal_position_id
                        resolution.reference_id = fiscal_position_id
                        return resolution
            resolution.status = ResolutionStatus.RESOLVED
            resolution.source = "odoo"
            resolution.resolution_method = PRODUCT_TAX
            resolution.value = product_tax_ids
            resolution.tax_ids = product_tax_ids
            resolution.reference_id = product_tax_ids[0]
            return resolution

        # Level 3: customer fiscal position mapping.
        if partner_id:
            fiscal_position_id = self._get_fiscal_position(partner_id)
            if fiscal_position_id:
                mapped = self._map_taxes(fiscal_position_id, product_tax_ids)
                resolution.status = ResolutionStatus.RESOLVED
                resolution.source = "odoo"
                resolution.resolution_method = CUSTOMER_FISCAL_POSITION
                resolution.value = mapped
                resolution.tax_ids = mapped
                resolution.fiscal_position_id = fiscal_position_id
                resolution.reference_id = fiscal_position_id
                return resolution

        # Level 4: company default sale taxes.
        default_taxes = self._default_sale_taxes()
        if default_taxes:
            resolution.status = ResolutionStatus.RESOLVED
            resolution.source = "odoo"
            resolution.resolution_method = COMPANY_DEFAULT
            resolution.value = default_taxes
            resolution.tax_ids = default_taxes
            resolution.reference_id = default_taxes[0]
            return resolution

        # Level 5: exception - nothing deterministic available.
        resolution.status = ResolutionStatus.UNRESOLVED
        resolution.reason = "tax_unresolved"
        return resolution

    @staticmethod
    def _as_ids(names: list[str]) -> list[int]:
        ids: list[int] = []
        for name in names:
            try:
                ids.append(int(name))
            except (TypeError, ValueError):
                continue
        return ids

    def _get_fiscal_position(self, partner_id: int) -> int | None:
        try:
            return self.odoo.get_partner_fiscal_position(partner_id)
        except Exception:
            logger.exception("tax.fiscal_position_lookup_failed", partner_id=partner_id)
            return None

    def _map_taxes(self, fiscal_position_id: int, tax_ids: list[int]) -> list[int]:
        try:
            mapped = self.odoo.map_taxes_through_fiscal_position(fiscal_position_id, tax_ids)
            return [int(t) for t in (mapped or [])]
        except Exception:
            logger.exception("tax.fiscal_map_failed", fiscal_position_id=fiscal_position_id)
            return []

    def _default_sale_taxes(self) -> list[int]:
        try:
            return [int(t) for t in (self.odoo.default_sale_taxes() or [])]
        except Exception:
            logger.exception("tax.default_lookup_failed")
            return []
