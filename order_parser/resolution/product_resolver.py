from __future__ import annotations

import math
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

# Unit canonicalization for pack comparison (product-side only).
PACK_UNIT_SYNONYMS = {
    "pcs": "pc", "pieces": "pc", "piece": "pc",
    "gms": "gm", "g": "gm", "gram": "gm", "grams": "gm",
    "kgs": "kg", "kilo": "kg", "kilos": "kg",
    "packets": "pkt", "packet": "pkt", "pack": "pkt", "packs": "pkt",
    "ltr": "l", "liter": "l", "litre": "l", "liters": "l",
    "mls": "ml", "inch": "in", "inches": "in",
}

# Bonus added for exact pack agreement (shared count+unit pairs), capped.
PACK_AGREEMENT_BONUS = 8.0

# Preparation/style words that never prove a mismatch on their own
# ("plain" missing from a candidate is a refinement question, not evidence
# the candidate is wrong; "kulcha" missing is).
GENERIC_MODIFIERS = frozenset({
    "plain", "fresh", "whole", "soft", "regular", "classic", "special",
    "premium", "fine", "rich", "plain",
})

# A token variant this similar to a winner token counts as covered
# ("bread" vs "breads"), so morphological plurals never veto.
VARIANT_SIMILARITY = 85.0


def canonical_unit(token: str) -> str:
    return PACK_UNIT_SYNONYMS.get(token, token)


def product_tokens(value: str | None) -> str:
    """Product-side normalization: shared normalization plus pack-size splits.

    Kept OUT of shared ``normalize_name`` on purpose: alias keys and customer
    matching depend on the stable shared form. Only product fuzzy scoring
    uses this.
    """
    text = normalize_name(value)
    for pattern in _GLUE_SPLIT:
        text = pattern.sub(r"\1 \2", text)
    return " ".join(canonical_unit(tok) for tok in text.split())


def pack_pairs(tokens: list[str]) -> list[tuple[float, str | None]]:
    """Extract (count, unit) pairs: a number optionally followed by a unit word."""
    pairs: list[tuple[float, str | None]] = []
    i = 0
    while i < len(tokens):
        try:
            number = float(tokens[i])
        except ValueError:
            i += 1
            continue
        unit = None
        if i + 1 < len(tokens) and tokens[i + 1].isalpha():
            unit = tokens[i + 1]
        pairs.append((number, unit))
        i += 2 if unit else 1
    return pairs


def pack_agreement(input_tokens: list[str], cand_tokens: list[str]) -> float:
    """0..1: share of the input's unit-qualified pack pairs found in candidate."""
    wanted = [(n, u) for n, u in pack_pairs(input_tokens) if u is not None]
    if not wanted:
        return 0.0
    have = set(pack_pairs(cand_tokens))
    hits = sum(1 for pair in wanted if pair in have)
    return hits / len(wanted)


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


def idf_weights(tokenized_docs: list[list[str]]) -> dict[str, float]:
    """Smoothed inverse document frequency over tokenized catalog names."""
    doc_count = max(1, len(tokenized_docs))
    doc_freq: dict[str, int] = {}
    for tokens in tokenized_docs:
        for token in set(tokens):
            doc_freq[token] = doc_freq.get(token, 0) + 1
    return {token: math.log((doc_count + 1) / (freq + 1)) + 1.0
            for token, freq in doc_freq.items()}


def idf_cosine_score(input_tokens: list[str], cand_tokens: list[str],
                     weights: dict[str, float]) -> float:
    """IDF-weighted cosine similarity (0-100): distinctive shared tokens win."""
    shared = set(input_tokens) & set(cand_tokens)
    if not shared:
        return 0.0
    numerator = sum(weights.get(tok, 1.0) ** 2 for tok in shared)
    input_norm = math.sqrt(sum(weights.get(tok, 1.0) ** 2 for tok in set(input_tokens)))
    cand_norm = math.sqrt(sum(weights.get(tok, 1.0) ** 2 for tok in set(cand_tokens)))
    if input_norm <= 0 or cand_norm <= 0:
        return 0.0
    return round(100.0 * numerator / (input_norm * cand_norm), 1)


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
        """Resolve one product name. Refreshes the catalog once on a miss so
        products created minutes ago (inside the TTL window) are still found."""
        result = self._resolve_inner(raw_name, partner_id)
        if result.status == ResolutionStatus.UNRESOLVED and result.reason not in (
                "missing_product_name", "catalog_unavailable"):
            try:
                before = len(self.catalog.products())
                self.catalog.refresh()
                after = len(self.catalog.products())
            except Exception:
                logger.exception("product.refresh_on_miss_failed", raw_name=raw_name)
                return result
            if after != before:
                logger.info("product.catalog_refreshed_on_miss",
                            raw_name=raw_name, before=before, after=after)
                return self._resolve_inner(raw_name, partner_id)
        return result

    def _resolve_inner(self, raw_name: str, partner_id: int | None = None) -> ProductResolution:
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
        # IDF-weighted cosine ranks first: distinctive shared tokens
        # (kulcha, pav) outweigh generic pack tokens (bread, pkt, 6), with a
        # bonus for exact pack agreement. When nothing clears the bar that
        # way, the legacy WRatio/partial blend preserves typo tolerance.
        docs = [product_tokens(str(p.get("name") or "")).split() for p in products]
        weights = idf_weights(docs)
        input_tokens = product_tokens(raw).split()
        idf_hits: list[tuple[float, dict]] = []
        legacy_hits: list[tuple[float, dict]] = []
        for product, cand_tokens in zip(products, docs):
            name = str(product.get("name") or "")
            cosine = idf_cosine_score(input_tokens, cand_tokens, weights)
            bonus = PACK_AGREEMENT_BONUS * pack_agreement(input_tokens, cand_tokens)
            idf_score = min(100.0, round(cosine + bonus, 1))
            _ts, legacy = score_product_pair(raw, name)
            sku = str(product.get("default_code") or "")
            if sku:
                legacy = max(legacy, score_product_pair(raw, sku)[1])
            if idf_score >= self.min_score:
                idf_hits.append((idf_score, product))
            elif legacy >= self.min_score:
                legacy_hits.append((round(float(legacy), 1), product))
        scored = idf_hits or legacy_hits
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
        vetoed = self._rare_token_veto(raw, input_tokens, best_product, products, weights)
        if vetoed is not None:
            return vetoed
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

    def _rare_token_veto(self, raw: str, input_tokens: list[str], best_product: dict,
                           products: list[dict], weights: dict[str, float]):
        """Veto a winner missing the input's rarest cataloged token.

        E.g. "Kulcha 250gm" must never auto-resolve to PAV when kulcha
        products exist: the rarest input token present in the catalog
        (kulcha) is absent from the winner while other products carry it.
        Returns an AMBIGUOUS resolution led by same-token alternatives, or
        None when the winner stands. Typos (token absent everywhere) never
        veto, preserving typo tolerance.
        """
        winner_tokens = set(product_tokens(str(best_product.get("name") or "")).split())
        input_set = set(input_tokens)
        if input_set <= winner_tokens:
            return None
        veto_token: str | None = None
        for tok in sorted(input_set - winner_tokens):
            if len(tok) <= 2 or tok in GENERIC_MODIFIERS:
                continue
            if max((normalized_fuzzy_score(tok, wtok) for wtok in winner_tokens),
                   default=0.0) >= VARIANT_SIMILARITY:
                continue
            doc_freq = sum(1 for p in products
                           if tok in set(product_tokens(str(p.get("name") or "")).split()))
            if doc_freq >= 1:
                veto_token = tok
                break
        if veto_token is None:
            return None
        rarest = veto_token
        alternatives = []
        for product in products:
            tokens = set(product_tokens(str(product.get("name") or "")).split())
            if rarest in tokens and product.get("id") != best_product.get("id"):
                alternatives.append(product)
        if not alternatives:
            return None
        logger.info("product.rare_token_veto", raw_name=raw, token=rarest,
                    winner=best_product.get("id"), alternatives=len(alternatives))
        ranked = sorted(
            alternatives,
            key=lambda p: (-idf_cosine_score(
                input_tokens, product_tokens(str(p.get("name") or "")).split(), weights),
                p.get("id") or 0),
        )
        candidates = [
            {"product_id": best_product["id"], "name": best_product.get("name"),
             "score": 0.0, "method": FUZZY_MATCH},
        ]
        for product in ranked[:MAX_CANDIDATES - 1]:
            candidates.append({"product_id": product["id"], "name": product.get("name"),
                               "score": 0.0, "method": "rare_token_match"})
        return self._ambiguous(candidates, raw, reason="rare_token_mismatch")

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
