from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import structlog

from order_parser.models import OrderModel
from order_parser.resolution.normalization import normalize_name

logger = structlog.get_logger(__name__)


def fingerprint_order(order: OrderModel) -> str:
    """Stable fingerprint of an order's commercial content for duplicate checks."""
    payload = {
        "customer": normalize_name(order.customer.name),
        "email": (order.customer.email or "").strip().casefold(),
        "items": sorted(
            [
                [
                    normalize_name(item.product_name),
                    round(float(item.quantity or 0), 3),
                    round(float(item.unit_price), 2) if item.unit_price is not None else None,
                ]
                for item in order.items
            ]
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DuplicateDetector:
    """Flags orders whose fingerprint was already ingested recently.

    Compares against pending/review records inside a configurable time
    window. Since Phase 6, successfully created orders are additionally
    covered by the persistent :class:`IdempotencyStore` history when
    ``enable_idempotency`` is on - this detector keeps handling the
    pending/review queue it can see.
    """

    def __init__(self, pending_store, window_hours: int = 24):
        self.pending_store = pending_store
        self.window = timedelta(hours=window_hours)

    def find_duplicate(self, fingerprint: str, exclude_order_id: str | None = None) -> str | None:
        if not fingerprint:
            return None
        now = datetime.now(timezone.utc)
        try:
            records = self.pending_store.list(status=None)
        except Exception:
            logger.exception("duplicate.scan_failed")
            return None
        for record in records:
            order_id = record.get("order_id")
            if not order_id or order_id == exclude_order_id:
                continue
            if record.get("fingerprint") != fingerprint:
                continue
            created_at = record.get("created_at") or record.get("resolved_at")
            if created_at:
                try:
                    stamp = datetime.fromisoformat(str(created_at))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    if now - stamp > self.window:
                        continue
                except ValueError:
                    pass
            logger.info("duplicate.detected", existing_order_id=order_id)
            return order_id
        return None
