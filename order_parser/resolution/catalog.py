from __future__ import annotations

import threading
import time

import structlog

from order_parser.resolution.normalization import normalize_name, normalize_sku, normalized_variants

logger = structlog.get_logger(__name__)

DEFAULT_TTL_SECONDS = 1800


class CatalogProvider:
    """Cached snapshot of the Odoo product catalog for deterministic matching.

    Wraps ``OdooClient.fetch_product_catalog()`` with a TTL cache and
    prebuilt lookup indexes (SKU and normalized name, including token-sorted
    variants). On fetch failure the previous snapshot is retained and the
    error re-raised so resolvers mark the catalog unavailable instead of
    guessing.
    """

    def __init__(self, odoo, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.odoo = odoo
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._products: list[dict] = []
        self._by_sku: dict[str, list[dict]] = {}
        self._by_name: dict[str, list[dict]] = {}
        self._loaded_at: float = 0.0

    def _ensure_fresh(self) -> None:
        if self._products and (time.time() - self._loaded_at) < self.ttl_seconds:
            return
        fetched = [
            p for p in self.odoo.fetch_product_catalog() if p.get("name") or p.get("default_code")
        ]
        by_sku: dict[str, list[dict]] = {}
        by_name: dict[str, list[dict]] = {}
        for product in fetched:
            sku = normalize_sku(product.get("default_code"))
            if sku:
                by_sku.setdefault(sku, []).append(product)
            for variant in normalized_variants(product.get("name")):
                by_name.setdefault(variant, []).append(product)
        self._products = fetched
        self._by_sku = by_sku
        self._by_name = by_name
        self._loaded_at = time.time()

    def refresh(self) -> None:
        with self._lock:
            self._loaded_at = 0.0
            self._products = []
            self._ensure_fresh()

    def products(self) -> list[dict]:
        with self._lock:
            try:
                self._ensure_fresh()
            except Exception:
                logger.exception("resolution.catalog_fetch_failed")
                raise
            return list(self._products)

    def by_sku(self) -> dict[str, list[dict]]:
        with self._lock:
            self._ensure_fresh()
            return dict(self._by_sku)

    def by_normalized_name(self) -> dict[str, list[dict]]:
        with self._lock:
            self._ensure_fresh()
            return dict(self._by_name)

    def get(self, product_id: int) -> dict | None:
        for product in self.products():
            if product.get("id") == product_id:
                return product
        return None
