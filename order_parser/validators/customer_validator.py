from __future__ import annotations

from typing import Any

import structlog

from order_parser.integrations.odoo_client import OdooClient
from order_parser.models import CustomerModel

logger = structlog.get_logger(__name__)


class CustomerValidator:
    """Checks customer information against Odoo partners.

    Existing customers resolve to their partner id; unknown customers are
    flagged for creation when the sales order is created.
    """

    def __init__(self, odoo: OdooClient):
        self.odoo = odoo

    def validate(self, customer: CustomerModel) -> dict[str, Any]:
        if not customer.name.strip() and not customer.email.strip() and not customer.phone.strip():
            return {"valid": False, "reason": "customer_info_missing"}
        if not self.odoo.enabled:
            return {"valid": False, "reason": "odoo_unavailable"}
        try:
            partner = self.odoo.find_partner(customer)
        except Exception:
            logger.exception("customer.partner_lookup_failed")
            return {"valid": False, "reason": "partner_lookup_failed"}
        if partner:
            return {
                "valid": True,
                "exists": True,
                "partner_id": partner["id"],
                "partner_name": partner["name"],
            }
        return {"valid": True, "exists": False}