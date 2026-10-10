from __future__ import annotations

import time
from typing import Any

import structlog
from rapidfuzz import process

from order_parser.integrations.odoo_client import OdooClient
from order_parser.models import ItemModel
from order_parser.resolution.matching import legacy_product_scorer

logger = structlog.get_logger(__name__)

# Calibrated against the spec examples: Dell Lattitude -> Dell Latitude (97),
# Keybord -> Keyboard (93), Lappy -> Laptop (75). Unrelated names score < 60.
DEFAULT_MIN_SCORE = 72.0
CATALOG_TTL_SECONDS = 1800


def _fuzzy_score(query: str, choice: str, **kwargs) -> float:
    return legacy_product_scorer(query, choice)


class ProductValidator:
    """Fuzzy-matches parsed items against the Odoo product catalog and checks
    quantity rules."""

    def __init__(
        self,
        odoo: OdooClient,
        min_score: float = DEFAULT_MIN_SCORE,
        catalog_ttl: int = CATALOG_TTL_SECONDS,
        auto_create: bool = False,
    ):
        self.odoo = odoo
        self.min_score = min_score
        self.catalog_ttl = catalog_ttl
        self.auto_create = auto_create
        self._catalog: list[dict[str, Any]] = []
        self._catalog_loaded_at = 0.0

    def _load_catalog(self) -> None:
        if self._catalog and (time.time() - self._catalog_loaded_at) < self.catalog_ttl:
            return
        self._catalog = [p for p in self.odoo.fetch_product_catalog() if p.get("name")]
        self._catalog_loaded_at = time.time()

    def validate(self, items: list[ItemModel]) -> list[dict[str, Any]]:
        if not self.odoo.enabled:
            return [
                {"product_name": item.product_name, "valid": False, "reason": "odoo_unavailable"}
                for item in items
            ]
        try:
            self._load_catalog()
        except Exception:
            logger.exception("product.catalog_fetch_failed")
            return [
                {"product_name": item.product_name, "valid": False, "reason": "catalog_unavailable"}
                for item in items
            ]

        names = [p["name"] for p in self._catalog]
        results: list[dict[str, Any]] = []
        for item in items:
            result: dict[str, Any] = {"product_name": item.product_name}
            if not item.product_name:
                result.update({"valid": False, "reason": "missing_product_name"})
                results.append(result)
                continue
            if item.quantity is None or float(item.quantity) <= 0:
                result.update({"valid": False, "reason": "quantity_must_be_positive"})
                results.append(result)
                continue
            match = process.extractOne(item.product_name, names, scorer=_fuzzy_score, score_cutoff=self.min_score)
            if match:
                product = self._catalog[match[2]]
                result.update(
                    {
                        "valid": True,
                        "product_id": product["id"],
                        "matched_name": product["name"],
                        "score": round(float(match[1]), 1),
                        "price": float(product.get("list_price") or 0.0),
                    }
                )
            elif self.auto_create and self.odoo.enabled:
                try:
                    product_id = self.odoo.create_product(
                        item.product_name,
                        price=item.unit_price if item.unit_price is not None else 0.0,
                    )
                    self._catalog.append({"id": product_id, "name": item.product_name})
                    result.update(
                        {
                            "valid": True,
                            "product_id": product_id,
                            "matched_name": item.product_name,
                            "score": 100.0,
                            "auto_created": True,
                            "price": float(item.unit_price) if item.unit_price is not None else 0.0,
                        }
                    )
                    logger.info("product.auto_created", name=item.product_name, product_id=product_id)
                except Exception:
                    logger.exception("product.auto_create_failed", name=item.product_name)
                    result.update({"valid": False, "reason": "product_not_found"})
            else:
                result.update({"valid": False, "reason": "product_not_found"})
            results.append(result)
        return results