from __future__ import annotations

import re
from typing import Any

from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel


def _to_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


class OrderNormalizer:
    """JSON Normalization Layer: converts any parsed input into the standard
    order schema (customer / items / metadata)."""

    @staticmethod
    def normalize(raw: dict[str, Any], source: str, input_type: str) -> OrderModel:
        customer_data = raw.get("customer", {}) or {}
        if isinstance(customer_data, str):
            customer_data = {"name": customer_data}
        items_data = raw.get("items", []) or []

        customer = CustomerModel(
            name=str(customer_data.get("name", "") or ""),
            email=str(customer_data.get("email", "") or ""),
            phone=str(customer_data.get("phone", "") or ""),
            address=str(customer_data.get("address", "") or ""),
            city=str(customer_data.get("city", "") or ""),
            state=str(customer_data.get("state", "") or ""),
            zip_code=str(customer_data.get("zip_code", "") or customer_data.get("zip", "") or ""),
            gstin=str(customer_data.get("gstin", "") or customer_data.get("vat", "") or customer_data.get("tax_id", "") or ""),
            country=str(customer_data.get("country", "") or ""),
        )

        normalized_items: list[ItemModel] = []
        for item in items_data:
            product_name = str(item.get("product_name", "") or item.get("product", "") or "").strip()
            if not product_name:
                continue
            quantity = _to_float(item.get("quantity"))
            normalized_items.append(
                ItemModel(
                    product_name=product_name,
                    quantity=quantity or 0,
                    unit_price=_to_float(item.get("unit_price")),
                    uom=str(item.get("uom", "Units") or "Units"),
                )
            )

        confidence = _to_float(raw.get("confidence"))
        if confidence is None:
            confidence = 0.0
        confidence = max(0.0, min(100.0, confidence))

        metadata = MetadataModel(
            source=source,
            input_type=input_type,
            confidence=confidence,
            notes=str(raw.get("notes", "") or ""),
        )

        # Canonical optional fields — pass through when AI provides them
        order_reference = raw.get("order_reference") or raw.get("reference") or None
        order_date = raw.get("order_date") or None
        delivery_date = raw.get("delivery_date") or raw.get("deliveryDate") or None
        billing_address = raw.get("billing_address") or None
        shipping_address = raw.get("shipping_address") or raw.get("delivery_address") or None
        currency = raw.get("currency") or None
        # Sender (vendor/collector) and deliver-to (buyer location) parties.
        # Accepted as a plain name string or a customer-shaped dict.
        sender_name = None
        sender_raw = raw.get("sender")
        if isinstance(sender_raw, dict):
            sender_name = str(sender_raw.get("name", "") or "").strip() or None
        elif sender_raw:
            sender_name = str(sender_raw).strip() or None
        deliver_to = None
        deliver_raw = (
            raw.get("deliver_to") or raw.get("delivery_to") or raw.get("ship_to") or raw.get("shipto")
        )
        if isinstance(deliver_raw, dict) and any(deliver_raw.values()):
            deliver_to = CustomerModel(
                name=str(deliver_raw.get("name", "") or ""),
                email=str(deliver_raw.get("email", "") or ""),
                phone=str(deliver_raw.get("phone", "") or ""),
                address=str(deliver_raw.get("address", "") or ""),
                city=str(deliver_raw.get("city", "") or ""),
                state=str(deliver_raw.get("state", "") or ""),
                zip_code=str(deliver_raw.get("zip_code", "") or deliver_raw.get("zip", "") or ""),
                gstin=str(deliver_raw.get("gstin", "") or deliver_raw.get("vat", "") or ""),
                country=str(deliver_raw.get("country", "") or ""),
            )
        # parser metadata when available
        parser_meta = None
        if raw.get("parser_metadata"):
            try:
                from order_parser.models import ParserMetadata

                parser_meta = ParserMetadata.model_validate(raw["parser_metadata"])
            except Exception:
                parser_meta = None

        return OrderModel(
            customer=customer,
            items=normalized_items,
            metadata=metadata,
            sender_name=sender_name,
            deliver_to=deliver_to,
            order_reference=str(order_reference) if order_reference else None,
            order_date=str(order_date) if order_date else None,
            delivery_date=str(delivery_date) if delivery_date else None,
            billing_address=str(billing_address) if billing_address else None,
            shipping_address=str(shipping_address) if shipping_address else None,
            currency=str(currency) if currency else None,
            confidence=confidence,
            parser_metadata=parser_meta,
        )