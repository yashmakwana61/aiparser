"""Central registry: internal codes -> user-facing explanations and actions.

Only codes the backend actually emits are mapped. Anything unmapped falls
back to a generic, safe definition (review + support reference) — never a
raw traceback or internal code name in user text.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ErrorDefinition:
    code: str
    title: str
    explanation: str
    solution: str
    solution_kind: str = "select"
    severity: str = "error"
    recoverable: bool = True
    needs_input: bool = False


def _d(code, title, explanation, solution, kind="select", severity="error", recoverable=True, needs_input=False):
    return ErrorDefinition(code, title, explanation, solution, kind, severity, recoverable, needs_input)


REGISTRY: dict[str, ErrorDefinition] = {
    # --- customer ---
    "CUSTOMER_UNRESOLVED": _d(
        "CUSTOMER_UNRESOLVED",
        "Customer needs confirmation",
        "I couldn't identify the customer in Odoo. Without a recognised customer, no sales order can be created.",
        "Select the correct customer below, or type the exact customer name as in Odoo.",
    ),
    "CUSTOMER_AMBIGUOUS": _d(
        "CUSTOMER_AMBIGUOUS",
        "Customer needs confirmation",
        "Several Odoo customers match this name, so I can't safely pick one on my own.",
        "Select the correct customer below, or type the exact customer name as in Odoo.",
    ),
    "COLLECTOR_AS_CUSTOMER": _d(
        "COLLECTOR_AS_CUSTOMER",
        "Sender is not the customer",
        "The name on the order belongs to a sender/distributor that forwards orders — it can never be the buying customer. I need the actual buyer's name.",
        "Type the buying customer's exact name as in Odoo, or pick below if options are shown.",
        needs_input=True,
    ),
    # --- product ---
    "PRODUCT_UNRESOLVED": _d(
        "PRODUCT_UNRESOLVED",
        "Product not found",
        "This product doesn't match anything in the Odoo catalog, so the order line can't be completed.",
        "Select the correct product below, or type the exact product name as in Odoo.",
    ),
    "PRODUCT_AMBIGUOUS": _d(
        "PRODUCT_AMBIGUOUS",
        "Product needs confirmation",
        "Multiple Odoo products match this name, so I can't safely pick one on my own.",
        "Select the correct product below, or type the exact product name as in Odoo.",
    ),
    # --- quantity ---
    "QUANTITY_CONFLICT": _d(
        "QUANTITY_CONFLICT",
        "Quantity required",
        "I found the product but couldn't determine a safe quantity for it.",
        "Reply with the numeric quantity for this item. Example: 25",
        kind="enter_number",
        needs_input=True,
    ),
    "INVALID_QUANTITY": _d(
        "INVALID_QUANTITY",
        "Quantity required",
        "The quantity on the order couldn't be safely interpreted as a number.",
        "Reply with the numeric quantity for this item. Example: 25",
        kind="enter_number",
        needs_input=True,
    ),
    # --- UOM ---
    "UOM_UNRESOLVED": _d(
        "UOM_UNRESOLVED",
        "Unit needs confirmation",
        "The unit on the order couldn't be matched to an Odoo unit. I won't guess a unit.",
        "Select the correct unit below, or type the unit name.",
    ),
    # --- price ---
    "PRICE_MISSING": _d(
        "PRICE_MISSING",
        "Price missing",
        "This item has no price and Odoo has no usable default for it.",
        "Reply with the unit price for this item. Example: 1450",
        kind="enter_number",
        needs_input=True,
    ),
    "PRICE_DEVIATION": _d(
        "PRICE_DEVIATION",
        "Price differs from Odoo",
        "The order price differs noticeably from the current Odoo price. Nothing has been changed.",
        "Choose which price to use for this order. The product master stays untouched.",
        kind="choose",
        severity="warning",
    ),
    # --- tax ---
    "TAX_UNRESOLVED": _d(
        "TAX_UNRESOLVED",
        "Tax needs confirmation",
        "I couldn't determine the correct tax for this item.",
        "Select the correct tax below.",
    ),
    "TAX_CONFLICT": _d(
        "TAX_CONFLICT",
        "Tax needs confirmation",
        "The detected tax disagrees with what Odoo suggests for this item.",
        "Confirm which tax to use. Tax configuration stays untouched.",
        kind="choose",
    ),
    "TAX_MISSING": _d(
        "TAX_MISSING",
        "Tax missing",
        "No tax could be determined for this item.",
        "Select the correct tax below.",
        severity="warning",
    ),
    # --- duplicates ---
    "DUPLICATE_ORDER": _d(
        "DUPLICATE_ORDER",
        "Possible duplicate",
        "A very similar order was already received. Creating again could bill the customer twice.",
        "Compare with the existing order, then create anyway only if you're sure it's a separate order.",
        kind="confirm",
        severity="warning",
    ),
    # --- OCR / documents ---
    "OCR_UNAVAILABLE": _d(
        "OCR_UNAVAILABLE",
        "Couldn't read this image",
        "The image-to-text service isn't available right now, so a scanned file can't be read yet.",
        "Upload the file again later, or send the order as text.",
        kind="upload",
    ),
    "OCR_EMPTY": _d(
        "OCR_EMPTY",
        "Couldn't read this image",
        "No readable text was found. The image may be blurry, cropped, or have glare/shadows.",
        "Upload a clearer image with the full order visible, or send the order as text.",
        kind="upload",
    ),
    "AI_INTERPRETATION_FAILED": _d(
        "AI_INTERPRETATION_FAILED",
        "Couldn't understand the order",
        "The document was received but the order details couldn't be confidently identified.",
        "Upload a clearer document, or send the order as text with customer, product and quantity.",
        kind="upload",
    ),
    "INPUT_TOO_LARGE": _d(
        "INPUT_TOO_LARGE",
        "File too large",
        "The file exceeds the maximum size and was stopped before processing.",
        "Send a smaller file, or split the order across messages.",
        kind="upload",
        recoverable=True,
    ),
    "INPUT_UNSUPPORTED": _d(
        "INPUT_UNSUPPORTED",
        "File type not supported",
        "This file type can't be processed. Text, images, PDFs and Excel files are supported.",
        "Send the order in a supported format.",
        kind="upload",
    ),
    # --- infrastructure ---
    "ODOO_UNAVAILABLE": _d(
        "ODOO_UNAVAILABLE",
        "Temporary system issue",
        "Your order was received safely, but Odoo can't be reached right now. You don't need to send it again.",
        "Wait for automatic retry, or retry now.",
        kind="retry",
        severity="warning",
    ),
    "ODOO_CREATE_FAILED": _d(
        "ODOO_CREATE_FAILED",
        "Order couldn't be created",
        "Odoo rejected the order creation. Your data is safe and nothing was half-created.",
        "Check the stated reason, fix the cause, then retry.",
        kind="retry",
    ),
    "TALLY_PENDING": _d(
        "TALLY_PENDING",
        "Tally sync pending",
        "The Odoo order exists. Tally synchronization is still pending.",
        "No action needed — this is informational. The Odoo order reference above is final.",
        kind="review",
        severity="info",
        recoverable=True,
    ),
    "SYS_TRANSIENT": _d(
        "SYS_TRANSIENT",
        "Temporary system issue",
        "Your order was received safely but a temporary problem interrupted processing. Nothing was created yet.",
        "Retry the operation. The reference below lets support trace it.",
        kind="retry",
        severity="warning",
    ),
    # --- fallback ---
    "INTERNAL_ERROR": _d(
        "INTERNAL_ERROR",
        "Something went wrong",
        "An unexpected problem stopped processing. The technical details were recorded for support.",
        "Try again. If it persists, share the support reference with your administrator.",
        kind="retry",
        recoverable=True,
    ),
}

GENERIC_DEFINITION = REGISTRY["INTERNAL_ERROR"]


def lookup(code: str) -> ErrorDefinition:
    return REGISTRY.get(code, GENERIC_DEFINITION)


# Blocking codes that mean "Odoo itself is the problem", not the data.
INFRA_BLOCKING_CODES = {"ODOO_UNAVAILABLE", "ODOO_CREATE_FAILED"}

# Warning codes that deserve an optional (non-blocking) user action.
WARNING_ACTIONABLE = {"PRICE_DEVIATION", "TAX_MISSING"}
