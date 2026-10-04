from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from order_parser.models import ItemModel, OrderModel


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


# Product matching methods.
SKU_EXACT = "sku_exact"
EXACT_NAME = "exact_name"
NORMALIZED_NAME = "normalized_name"
GLOBAL_ALIAS = "global_alias"
CUSTOMER_ALIAS = "customer_alias"

# Customer matching methods (explicit id / session / staff selection come first).
EXPLICIT_ID = "explicit_id"
SESSION_CUSTOMER = "session_customer"
STAFF_SELECTED = "staff_selected"
EMAIL_EXACT = "email_exact"
PHONE_EXACT = "phone_exact"
ALIAS_MATCH = "alias_match"

FUZZY_MATCH = "fuzzy_match"

# UOM methods.
EXPLICIT_UOM = "explicit_uom"
PRODUCT_SALES_UOM = "product_sales_uom"
APPROVED_CONVERSION = "approved_conversion"

# Price methods.
EXPLICIT_ORDER_PRICE = "explicit_order_price"
CUSTOMER_PRICELIST = "customer_pricelist"
PRODUCT_SALES_PRICE = "product_sales_price"
APPROVED_FALLBACK = "approved_fallback"

# Tax methods.
EXPLICIT_TAX = "explicit_tax"
PRODUCT_TAX = "product_tax"
CUSTOMER_FISCAL_POSITION = "customer_fiscal_position"
COMPANY_DEFAULT = "company_default"

# Human-confirmed override from the correction flow (Telegram picker).
# Authoritative for the order; never modifies Odoo master data.
HUMAN_OVERRIDE = "human_override"

# Methods that may support automatic order creation; fuzzy matches never do.
DETERMINISTIC_METHODS = {
    SKU_EXACT,
    EXACT_NAME,
    NORMALIZED_NAME,
    GLOBAL_ALIAS,
    CUSTOMER_ALIAS,
    EXPLICIT_ID,
    SESSION_CUSTOMER,
    STAFF_SELECTED,
    EMAIL_EXACT,
    PHONE_EXACT,
    ALIAS_MATCH,
    HUMAN_OVERRIDE,
}

# Fuzzy-only matches are capped below AUTO_CREATE_THRESHOLD so they land in the
# confirmation band instead of ever auto-creating.
DEFAULT_FUZZY_CONFIDENCE_CAP = 89.0

# Blocking issue codes surfaced by the resolution layer.
PRODUCT_AMBIGUOUS = "PRODUCT_AMBIGUOUS"
PRODUCT_UNRESOLVED = "PRODUCT_UNRESOLVED"
CUSTOMER_AMBIGUOUS = "CUSTOMER_AMBIGUOUS"
CUSTOMER_UNRESOLVED = "CUSTOMER_UNRESOLVED"
# Extracted customer is a known vendor/collector, never a real customer.
COLLECTOR_AS_CUSTOMER = "COLLECTOR_AS_CUSTOMER"
UOM_UNRESOLVED = "UOM_UNRESOLVED"
PRICE_MISSING = "PRICE_MISSING"
TAX_CONFLICT = "TAX_CONFLICT"
TAX_UNRESOLVED = "TAX_UNRESOLVED"
QUANTITY_CONFLICT = "QUANTITY_CONFLICT"
DUPLICATE_ORDER = "DUPLICATE_ORDER"
RESOLUTION_FAILED = "RESOLUTION_FAILED"

# Non-gating warning codes.
PRICE_DEVIATION = "PRICE_DEVIATION"
PRICE_FALLBACK = "PRICE_FALLBACK"

# Phase 18: Informational warnings for missing non-blocking fields.
UOM_MISSING_WARNING = "UOM_MISSING"
PRICE_MISSING_WARNING = "PRICE_MISSING"
TAX_MISSING_WARNING = "TAX_MISSING"


class FieldResolution(BaseModel):
    """Resolution metadata retained for every resolved field.

    ``source`` is one of: odoo | order | alias_store | config | none.
    Named reference ids mirror the spec examples (e.g. ``pricelist_id``).
    """

    status: ResolutionStatus = ResolutionStatus.UNRESOLVED
    source: str = "none"
    resolution_method: str | None = None
    value: Any = None
    confidence: float | None = None
    reason: str | None = None
    reference_id: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ProductResolution(FieldResolution):
    raw_name: str = ""
    product_id: int | None = None
    product_name: str | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class CustomerResolution(FieldResolution):
    partner_id: int | None = None
    partner_name: str | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class UOMResolution(FieldResolution):
    parsed_uom: str = ""
    uom_id: int | None = None
    uom_name: str | None = None
    conversion_factor: float = 1.0


class PriceResolution(FieldResolution):
    unit_price: float | None = None
    pricelist_id: int | None = None


class TaxResolution(FieldResolution):
    tax_ids: list[int] = Field(default_factory=list)
    explicit_tax_names: list[str] = Field(default_factory=list)
    fiscal_position_id: int | None = None


class BlockingIssue(BaseModel):
    code: str
    message: str
    item_index: int | None = None


class ResolvedItem(BaseModel):
    index: int
    item: ItemModel
    quantity_effective: float = 0.0
    product: ProductResolution = Field(default_factory=ProductResolution)
    uom: UOMResolution = Field(default_factory=UOMResolution)
    price: PriceResolution = Field(default_factory=PriceResolution)
    tax: TaxResolution = Field(default_factory=TaxResolution)
    missing_fields: list[str] = Field(default_factory=list)


class ResolvedOrder(BaseModel):
    """Result of the master data resolution layer for a single parsed order."""

    order: OrderModel
    customer: CustomerResolution = Field(default_factory=CustomerResolution)
    items: list[ResolvedItem] = Field(default_factory=list)
    warnings: list[BlockingIssue] = Field(default_factory=list)
    blocking_issues: list[BlockingIssue] = Field(default_factory=list)
    fingerprint: str = ""
    resolved_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def is_auto_eligible(self) -> bool:
        """True only when deterministic validation passed end to end.

        High AI confidence never overrides this gate; fuzzy-matched products or
        customers, approved fallback prices and any blocking issue disqualify it.
        """
        if self.blocking_issues:
            return False
        if self.customer.resolution_method not in DETERMINISTIC_METHODS:
            return False
        for item in self.items:
            if item.product.resolution_method not in DETERMINISTIC_METHODS:
                return False
            if item.price.resolution_method == APPROVED_FALLBACK:
                return False
        return True

    @property
    def is_auto_eligible_including_high_confidence_fuzzy(self) -> bool:
        """True when all fuzzy matches have confidence >= 90 (post-normalization).

        Used by AUTO_CREATE_ALL_ORDERS to allow auto-creation when products
        are resolved via high-confidence fuzzy matching (normalized names
        match closely) but are not strictly deterministic.
        """
        if self.blocking_issues:
            return False
        if self.customer.resolution_method not in DETERMINISTIC_METHODS:
            if self.customer.resolution_method != FUZZY_MATCH:
                return False
            if (self.customer.confidence or 0) < 90:
                return False
        for item in self.items:
            method = item.product.resolution_method
            if method not in DETERMINISTIC_METHODS:
                if method != FUZZY_MATCH:
                    return False
                if (item.product.confidence or 0) < 90:
                    return False
            if item.price.resolution_method == APPROVED_FALLBACK:
                return False
        return True

    @property
    def missing_information(self) -> list[str]:
        """Aggregated unique missing fields across all items (sorted)."""
        info: list[str] = []
        for item in self.items:
            info.extend(item.missing_fields)
        return sorted(set(info))

    @property
    def has_blocking_for_odoo(self) -> bool:
        """True when creation-blocking issues prevent an Odoo Sales Order."""
        return bool(self.blocking_issues)

    @property
    def has_blocking_for_tally(self) -> bool:
        """Informational: missing financial fields mean Tally sync is delayed.

        The final Tally readiness decision MUST be made in Odoo, not here.
        """
        return bool(self.missing_information)

    def summary(self) -> dict[str, Any]:
        """Compact JSON-safe view stored in audit entries and pending records."""
        return {
            "customer": {
                "status": self.customer.status.value,
                "method": self.customer.resolution_method,
                "partner_id": self.customer.partner_id,
                "partner_name": self.customer.partner_name,
                "confidence": self.customer.confidence,
            },
            "items": [
                {
                    "index": item.index,
                    "raw_name": item.product.raw_name,
                    "status": item.product.status.value,
                    "method": item.product.resolution_method,
                    "product_id": item.product.product_id,
                    "confidence": item.product.confidence,
                    "uom_method": item.uom.resolution_method,
                    "uom_id": item.uom.uom_id,
                    "quantity_effective": item.quantity_effective,
                    "price_method": item.price.resolution_method,
                    "unit_price": item.price.unit_price,
                    "tax_method": item.tax.resolution_method,
                    "tax_ids": item.tax.tax_ids,
                    "missing_fields": item.missing_fields,
                }
                for item in self.items
            ],
            "blocking": [issue.code for issue in self.blocking_issues],
            "blocking_detail": [
                {"code": issue.code, "message": issue.message} for issue in self.blocking_issues
            ],
            "warnings": [warning.code for warning in self.warnings],
            "missing_information": self.missing_information,
            "auto_eligible": self.is_auto_eligible,
            "blocking_for_odoo": self.has_blocking_for_odoo,
            "blocking_for_tally": self.has_blocking_for_tally,
            "fingerprint": self.fingerprint,
        }
