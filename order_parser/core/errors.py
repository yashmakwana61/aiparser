from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ErrorCode(str, Enum):
    PARSER_001 = "PARSER-001"  # Unable to extract order
    OCR_001 = "OCR-001"  # OCR service unavailable
    OCR_002 = "OCR-002"  # Unable to extract usable text
    RES_001 = "RES-001"  # Customer unresolved
    RES_002 = "RES-002"  # Product unresolved
    RES_003 = "RES-003"  # Ambiguous product
    ODOO_001 = "ODOO-001"  # Odoo unavailable
    ODOO_002 = "ODOO-002"  # Odoo order creation failed
    SYS_001 = "SYS-001"  # Unexpected parser error
    VALIDATION_001 = "VALIDATION-001"
    RATE_LIMIT_001 = "RATE-LIMIT-001"
    RESOURCE_001 = "RESOURCE-001"  # File too large / limits exceeded
    IDEMPOTENCY_001 = "IDEMPOTENCY-001"  # Duplicate


ERROR_CATEGORY_MAP: dict[str, ErrorCategory] = {
    ErrorCode.PARSER_001: ErrorCategory.REVIEW_REQUIRED,
    ErrorCode.OCR_001: ErrorCategory.TRANSIENT,
    ErrorCode.OCR_002: ErrorCategory.REVIEW_REQUIRED,
    ErrorCode.RES_001: ErrorCategory.REVIEW_REQUIRED,
    ErrorCode.RES_002: ErrorCategory.REVIEW_REQUIRED,
    ErrorCode.RES_003: ErrorCategory.REVIEW_REQUIRED,
    ErrorCode.ODOO_001: ErrorCategory.TRANSIENT,
    ErrorCode.ODOO_002: ErrorCategory.TRANSIENT,
    ErrorCode.SYS_001: ErrorCategory.TRANSIENT,
    ErrorCode.VALIDATION_001: ErrorCategory.PERMANENT,
    ErrorCode.RATE_LIMIT_001: ErrorCategory.TRANSIENT,
    ErrorCode.RESOURCE_001: ErrorCategory.PERMANENT,
    ErrorCode.IDEMPOTENCY_001: ErrorCategory.PERMANENT,
}


def classify_error(code: str) -> ErrorCategory:
    return ERROR_CATEGORY_MAP.get(code, ErrorCategory.TRANSIENT)


def is_retryable(code: str) -> bool:
    return classify_error(code) == ErrorCategory.TRANSIENT
