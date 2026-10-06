from __future__ import annotations

import re

import structlog
from rapidfuzz import fuzz

from order_parser.config import get_settings
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.models import (
    CUSTOMER_ALIAS,
    EXACT_NAME,
    FUZZY_MATCH,
    GLOBAL_ALIAS,
    NORMALIZED_NAME,
    SKU_EXACT,
    DEFAULT_FUZZY_CONFIDENCE_CAP,
    ProductResolution,
    ResolutionStatus,
)
from order_parser.resolution.normalization import normalize_name, normalize_sku, normalized_variants

logger = structlog.get_logger(__name__)

MAX_CANDIDATES = 5


def fuzzy_score(query: str, choice: str) -> float:
    if not choice:
        return 0.0
    return max(fuzz.WRatio(query, choice), fuzz.partial_ratio(query, choice))


def normalized_fuzzy_score(query: str, choice: str) -> float:
    """Fuzzy score after normalizing both strings (case-insensitive, punctuation removed)."""
    nq = normalize_name(query)
    nc = normalize_name(choice)
    if not nq or not nc:
        return 0.0
    return max(fuzz.WRatio(nq, nc), fuzz.partial_ratio(nq, nc))


# Digit/letter glue split for FMCG pack sizes: "6pcs" -> "6 pcs".
_GLUE_SPLIT = (
    re.compile(r"(\d)([a-zA-Z])"),
    re.compile(r"([a-zA-Z])(\d)"),
)


def product_tokens(value: str | None) -> str:
    """Product-side normalization: shared normalization plus pack-size splits.

    Kept OUT of shared ``normalize_name`` on purpose: alias keys and customer
    matching depend on the stable shared form. Only product fuzzy scoring
    uses this.
    """
    text = normalize_name(value)
    for pattern in _GLUE_SPLIT:
        text = pattern.sub(r"\1 \2", text)
    return " ".join(text.split())


def product_fuzzy_score(query: str, choice: str) -> float:
    """Pack-size-aware product similarity (0-100).

    Prefers ``token_set_ratio`` (shared distinctive tokens over the whole
    string) and falls back to the legacy WRatio/partial blend (typo
    tolerance, e.g. "Lappy" vs "Laptop 15"). See ``score_product_pair``.
    """
    ts, legacy = score_product_pair(query, choice)
    return max(ts, legacy)


def score_product_pair(query: str, choice: str) -> tuple[float, float]:
    """Return ``(token_set_score, legacy_score)`` for a product pair."""
    nq, nc = product_tokens(query), product_tokens(choice)
    if not nq or not nc:
        return 0.0, 0.0
    token_set = float(fuzz.token_set_ratio(nq, nc))
    legacy = float(max(fuzz.WRatio(nq, nc), fuzz.partial_ratio(nq, nc)))
    return token_set, legacy


class ProductResolver:
    """Deterministic product identity resolution against the Odoo catalog.

    Hierarchy: SKU -> exact name -> normalized name -> global alias ->
    customer-specific alias -> fuzzy match (capped below the auto-create band)
    -> candidate ranking with ambiguity detection. The resolver never guesses:
    close fuzzy ties return ``ambiguous`` and misses return ``unresolved``.
    It never writes to Odoo and never creates products or aliases.
    """

    def __init__(
        self,
        catalog: CatalogProvider,
        aliases: AliasStore | None = None,
        settings=None,
    ):
        self.catalog = catalog
        self.aliases = aliases
        self.settings = settings or get_settings()
        self.min_score = float(self.settings.resolution_fuzzy_cutoff)
        self.ambiguity_gap = float(self.settings.resolution_ambiguity_gap)
        self.confidence_cap = float(
            getattr(self.settings, "resolution_fuzzy_confidence_cap", DEFAULT_FUZZY_CONFIDENCE_CAP)
        )

    def resolve(self, raw_name: str, partner_id: int | None = None) -> ProductResolution:
        raw = (raw_name or "").strip()
        if not raw:
            return ProductResolution(raw_name="", status=ResolutionStatus.UNRESOLVED, reason="missing_product_name")
        try:
            sku_index = self.catalog.by_sku()
            name_index = self.catalog.by_normalized_name()
            products = self.catalog.products()
        except Exception:
            logger.exception("product.resolve_catalog_unavailable", raw_name=raw)
            return ProductResolution(raw_name=raw, status=ResolutionStatus.UNRESOLVED, reason="catalog_unavailable")

        # Level 1: Odoo internal reference (SKU).
        sku_key = normalize_sku(raw)
        if sku_key and len(sku_index.get(sku_key, [])) == 1:
            product = sku_index[sku_key][0]
            return self._resolved(product, SKU_EXACT, 100.0, raw)

        # Level 2: exact product name (case-insensitive literal).
        exact_hits = [p for p in products if raw.casefold() == str(p.get("name") or "").strip().casefold()]
        if len(exact_hits) == 1:
            return self._resolved(exact_hits[0], EXACT_NAME, 100.0, raw)
        if len(exact_hits) > 1:
            return self._ambiguous(
                [{"product_id": p["id"], "name": p["name"], "score": 100.0, "method": EXACT_NAME} for p in exact_hits],
                raw,
                reason="multiple_products_share_this_name",
            )

        # Level 3: normalized name, then token-sorted variant.
        variants = normalized_variants(raw)
        normalized_hits = name_index.get(variants[0], []) if variants else []
        if len(normalized_hits) == 1:
            return self._resolved(normalized_hits[0], NORMALIZED_NAME, 99.0, raw)
        sorted_form = variants[1] if len(variants) > 1 else ""
        sorted_hits = name_index.get(sorted_form, []) if sorted_form else []
        if len(sorted_hits) == 1:
            return self._resolved(sorted_hits[0], NORMALIZED_NAME, 99.0, raw)

        # Level 4/5: alias lookup (customer-scoped wins over global).
        if self.aliases is not None:
            alias = self.aliases.find_product(normalized_variants(raw)[0], customer_id=partner_id)
            if alias is not None:
                method = CUSTOMER_ALIAS if alias.customer_id else GLOBAL_ALIAS
                confidence = 97.0 if alias.customer_id else 98.0
                product = next((p for p in products if p["id"] == alias.target_id), None)
                if product is not None:
                    self.aliases.record_usage("product", alias.id)
                    return self._resolved(product, method, confidence, raw)
                logger.warning("product.alias_target_missing", alias_id=alias.id, target_id=alias.target_id)

        # Level 6-8: fuzzy matching with candidate ranking and ambiguity gate.
        # Token-set matches (shared distinctive tokens over the whole
        # string) rank first; when nothing clears the bar that way, the
        # legacy WRatio/partial blend preserves typo tolerance.
        token_set_hits: list[tuple[float, dict]] = []
        legacy_hits: list[tuple[float, dict]] = []
        for product in products:
            name = str(product.get("name") or "")
            token_set, legacy = score_product_pair(raw, name)
            sku = str(product.get("default_code") or "")
            if sku:
                sku_token_set, sku_legacy = score_product_pair(raw, sku)
                token_set = max(token_set, sku_token_set)
                legacy = max(legacy, sku_legacy)
            if token_set >= self.min_score:
                token_set_hits.append((round(float(token_set), 1), product))
            elif legacy >= self.min_score:
                legacy_hits.append((round(float(legacy), 1), product))
        scored = token_set_hits or legacy_hits
        scored.sort(key=lambda pair: (-pair[0], pair[1].get("id") or 0))

        candidates = [
            {"product_id": p["id"], "name": p["name"], "score": s, "method": FUZZY_MATCH}
            for s, p in scored[:MAX_CANDIDATES]
        ]
        if not scored:
            return ProductResolution(raw_name=raw, status=ResolutionStatus.UNRESOLVED, reason="no_candidate_above_cutoff")
        best_score, best_product = scored[0]
        if len(scored) > 1 and (best_score - scored[1][0]) <= self.ambiguity_gap:
            return self._ambiguous(candidates, raw, reason="fuzzy_candidates_too_close")
        return ProductResolution(
            raw_name=raw,
            status=ResolutionStatus.RESOLVED,
            source="odoo",
            resolution_method=FUZZY_MATCH,
            value=best_product.get("name"),
            confidence=min(best_score, self.confidence_cap),
            reference_id=best_product.get("id"),
            product_id=best_product.get("id"),
            product_name=best_product.get("name"),
            candidates=candidates,
            details={
                "default_code": best_product.get("default_code"),
                "list_price": best_product.get("list_price"),
                "uom_id": best_product.get("uom_id"),
                "taxes_id": list(best_product.get("taxes_id") or []),
            },
        )

    @staticmethod
    def _resolved(product: dict, method: str, confidence: float, raw_name: str) -> ProductResolution:
        return ProductResolution(
            raw_name=raw_name,
            status=ResolutionStatus.RESOLVED,
            source="odoo",
            resolution_method=method,
            value=product.get("name"),
            confidence=confidence,
            reference_id=product.get("id"),
            product_id=product.get("id"),
            product_name=product.get("name"),
            details={
                "default_code": product.get("default_code"),
                "list_price": product.get("list_price"),
                "uom_id": product.get("uom_id"),
                "taxes_id": list(product.get("taxes_id") or []),
            },
        )

    @staticmethod
    def _ambiguous(candidates: list[dict], raw_name: str, reason: str) -> ProductResolution:
        return ProductResolution(
            raw_name=raw_name,
            status=ResolutionStatus.AMBIGUOUS,
            source="odoo",
            resolution_method=None,
            value=None,
            reason=reason,
            candidates=candidates,
        )
