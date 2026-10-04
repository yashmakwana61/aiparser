"""Registry: every mapped code has safe user text; unknown codes degrade safely."""

from order_parser.user_actions.registry import (
    GENERIC_DEFINITION,
    INFRA_BLOCKING_CODES,
    REGISTRY,
    WARNING_ACTIONABLE,
    lookup,
)

# Codes the backend actually emits (must all be mapped).
EXPECTED_CODES = {
    "CUSTOMER_UNRESOLVED", "CUSTOMER_AMBIGUOUS", "COLLECTOR_AS_CUSTOMER",
    "PRODUCT_AMBIGUOUS", "PRODUCT_UNRESOLVED",
    "QUANTITY_CONFLICT", "INVALID_QUANTITY",
    "UOM_UNRESOLVED", "PRICE_MISSING", "PRICE_DEVIATION",
    "TAX_UNRESOLVED", "TAX_CONFLICT", "TAX_MISSING",
    "DUPLICATE_ORDER", "OCR_UNAVAILABLE", "OCR_EMPTY",
    "AI_INTERPRETATION_FAILED", "INPUT_TOO_LARGE", "INPUT_UNSUPPORTED",
    "ODOO_UNAVAILABLE", "ODOO_CREATE_FAILED", "TALLY_PENDING",
    "SYS_TRANSIENT", "INTERNAL_ERROR",
}


def test_all_real_codes_mapped():
    missing = EXPECTED_CODES - set(REGISTRY)
    assert not missing, f"unmapped codes: {missing}"


def test_user_text_never_leaks_internals():
    banned = ("traceback", "Traceback", "Exception", "sql", "SELECT",
              "api_key", "API key", "token", "/app/", "odoo_client.py")
    for code, definition in REGISTRY.items():
        blob = f"{definition.title}\n{definition.explanation}\n{definition.solution}"
        for token in banned:
            assert token not in blob, f"{code} leaks {token!r}"
        assert definition.title and definition.explanation and definition.solution


def test_unknown_code_falls_back_to_generic():
    assert lookup("SOME_FUTURE_CODE").code == "INTERNAL_ERROR"
    assert lookup("") is GENERIC_DEFINITION


def test_infra_and_warning_sets():
    assert {"ODOO_UNAVAILABLE", "ODOO_CREATE_FAILED"} <= INFRA_BLOCKING_CODES
    assert "PRICE_DEVIATION" in WARNING_ACTIONABLE
    # Recoverable problems must offer a path forward, not a dead end.
    for code in ("CUSTOMER_AMBIGUOUS", "PRODUCT_AMBIGUOUS", "DUPLICATE_ORDER",
                 "ODOO_UNAVAILABLE", "OCR_EMPTY"):
        assert REGISTRY[code].recoverable
