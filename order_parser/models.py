from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CustomerModel(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    gstin: str = ""
    country: str = ""


class ItemModel(BaseModel):
    product_name: str = ""
    quantity: int | float = 0
    unit_price: float | None = None
    uom: str | None = None
    product_id: int | None = None
    # Canonical extensions (backward-compatible, optional)
    discount: float | None = None
    confidence: float | None = None
    notes: str | None = None


class MetadataModel(BaseModel):
    source: str = ""
    input_type: str = ""
    confidence: float = 0.0
    notes: str = ""


class ParserMetadata(BaseModel):
    """Provenance for an order (parser/AI/OCR versions)."""

    parser_version: str = "1.2.0"
    model_version: str = ""
    ocr_provider: str = ""
    schema_version: str = "1.0"


class OrderModel(BaseModel):
    customer: CustomerModel = Field(default_factory=CustomerModel)
    items: list[ItemModel] = Field(default_factory=list)
    metadata: MetadataModel = Field(default_factory=MetadataModel)
    # Vendor / sender / order-collector party as extracted from the document.
    # A sender is NEVER the customer; resolution reroutes to deliver_to or
    # raises COLLECTOR_AS_CUSTOMER when the customer slot holds one.
    sender_name: str | None = None
    deliver_to: CustomerModel | None = None
    # Canonical extensions — all optional for backward compatibility
    order_reference: str | None = None
    order_date: str | None = None
    delivery_date: str | None = None
    billing_address: str | None = None
    shipping_address: str | None = None
    currency: str | None = None
    confidence: float | None = None
    missing_fields: list[str] = Field(default_factory=list)
    parser_metadata: ParserMetadata | None = None


class AIExtractedOrder(BaseModel):
    customer: dict[str, Any] = Field(default_factory=dict)
    items: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = ""
    confidence: float = 0.0


class ParsedOrder(BaseModel):
    """Result of a processor: the normalized order plus raw extraction context.

    ``ai_response`` is the raw AI gateway JSON (empty for deterministic parses such as
    a direct Excel mapping) and ``extracted_text`` is the text the AI saw.
    """

    order: OrderModel
    ai_response: dict[str, Any] = Field(default_factory=dict)
    extracted_text: str = ""


class ValidationResult(BaseModel):
    is_valid: bool = True
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OdooPayload(BaseModel):
    customer: CustomerModel
    items: list[ItemModel]
    metadata: MetadataModel
    source: str = "telegram"
    confidence: float = 0.0


class ApiResponse(BaseModel):
    status: Literal["success", "error", "pending"] = "success"
    sales_order: str | None = None
    customer: str | None = None
    items: int = 0
    message: str | None = None


# ------------------------------------------------------------------
# Phase 18: Permissive resolution output contract.
# ------------------------------------------------------------------

ReadinessStatus = Literal[
    "READY_FOR_ODOO",
    "MISSING_CUSTOMER",
    "MISSING_ORDER_DETAILS",
    "PRODUCT_AMBIGUOUS",
    "PRODUCT_UNKNOWN",
    "INVALID_QUANTITY",
    "PARSER_FAILURE",
    "OCR_FAILURE",
    "ODOO_UNAVAILABLE",
    "success",
    "error",
    "pending",
    "review",
]


class CustomerPayload(BaseModel):
    raw_name: str = ""
    resolved: bool = False
    partner_id: int | None = None
    partner_name: str | None = None


class ItemPayload(BaseModel):
    raw_name: str = ""
    product_id: int | None = None
    product_name: str | None = None
    quantity: float = 0
    uom: str | None = None
    price: float | None = None
    tax_ids: list[int] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class OrderReadinessResponse(BaseModel):
    """Stable API response for order readiness (Phase 18)."""

    request_id: str = ""
    session_id: str | None = None
    status: ReadinessStatus = "READY_FOR_ODOO"
    channel: str = "api"

    customer: CustomerPayload | None = None
    items: list[ItemPayload] = Field(default_factory=list)

    missing_information: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    blocking_for_odoo: bool = False
    blocking_for_tally: bool = False

    # Backward-compatible fields for existing callers.
    sales_order: str | None = None
    order_id: str | None = None
    confidence: float = 0.0
    message: str | None = None
