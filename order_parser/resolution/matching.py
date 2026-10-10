"""Central RapidFuzz matching layer: one scorer per field, one calling convention.

All fuzzy comparison in the parser goes through here so scoring is
consistent, fast (``process.extract`` with engine-side cutoffs), and tuned
per field:

- product names: IDF-weighted cosine + pack agreement, legacy blend fallback
- customer names: token-sort ratio (word-order tolerant)
- SKUs/codes: plain ratio (partial matching inflates short codes)
- GSTIN/email/phone: exact only — never fuzzy (enforced by callers)

Scorers take raw strings; normalization lives in exactly one processor per
field so indexed and query text always agree.
"""

from __future__ import annotations

import math
import re

from rapidfuzz import fuzz, process

from order_parser.resolution.normalization import normalize_name

# --------------------------------------------------------------------------
# Product-side normalization (pack sizes). Kept OUT of shared
# ``normalize_name`` on purpose: alias keys and customer matching depend on
# the stable shared form.

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


def canonical_unit(token: str) -> str:
    return PACK_UNIT_SYNONYMS.get(token, token)


def product_tokens(value: str | None) -> str:
    """Product-side normalization: shared normalization plus pack-size splits."""
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


# --------------------------------------------------------------------------
# IDF rarity weighting over a product-name corpus.


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


# --------------------------------------------------------------------------
# Scorers (raw strings in, 0-100 out). Pair each with its processor.


def customer_scorer(query: str, choice: str, **kwargs) -> float:
    """Customer names: token-sort ratio (word-order tolerant).

    Tuned alternative to :func:`legacy_customer_scorer`. Currently opt-in
    only: production evidence shows the legacy blend preserves the
    ambiguity ties (ITC/Maurya/Tauru, Radisson units) that the safety
    layer depends on, while token-sort over-separates them into
    single-winner resolutions. Do not make it the default without
    golden-set proof.
    """
    if not query or not choice:
        return 0.0
    return float(fuzz.token_sort_ratio(query, choice))


def legacy_customer_scorer(query: str, choice: str, **kwargs) -> float:
    """Customer names default: WRatio/partial blend (ambiguity-preserving)."""
    if not query or not choice:
        return 0.0
    return float(max(fuzz.WRatio(query, choice), fuzz.partial_ratio(query, choice)))


def normalized_fuzzy_score(query: str, choice: str) -> float:
    """Fuzzy score after normalizing both strings (case-insensitive, punctuation removed)."""
    nq = normalize_name(query)
    nc = normalize_name(choice)
    if not nq or not nc:
        return 0.0
    return max(fuzz.WRatio(nq, nc), fuzz.partial_ratio(nq, nc))


def sku_scorer(query: str, choice: str, **kwargs) -> float:
    """Codes: plain ratio only — partial matching inflates short codes."""
    if not query or not choice:
        return 0.0
    return float(fuzz.ratio(query, choice))


def legacy_product_scorer(query: str, choice: str, **kwargs) -> float:
    """Typo-tolerant fallback blend for product names."""
    if not query or not choice:
        return 0.0
    return float(max(fuzz.WRatio(query, choice), fuzz.partial_ratio(query, choice)))


def product_fuzzy_score(query: str, choice: str) -> float:
    """Pack-size-aware product similarity (0-100). See ``score_product_pair``."""
    token_set, legacy = score_product_pair(query, choice)
    return max(token_set, legacy)


def score_product_pair(query: str, choice: str) -> tuple[float, float]:
    """Return ``(token_set_score, legacy_score)`` for a product pair."""
    nq, nc = product_tokens(query), product_tokens(choice)
    if not nq or not nc:
        return 0.0, 0.0
    token_set = float(fuzz.token_set_ratio(nq, nc))
    legacy = legacy_product_scorer(nq, nc)
    return token_set, legacy


# --------------------------------------------------------------------------
# Ranked extraction with engine-side cutoffs.


def top_matches(names: list[str], query: str, scorer, cutoff: float,
                limit: int | None = None, processor=None) -> list[tuple[float, int]]:
    """``[(score, index)]`` sorted best-first via ``process.extract``.

    The cutoff is enforced inside the engine, so large catalogs never pay
    for hopeless pairs.
    """
    if not query or not names:
        return []
    hits = process.extract(
        query, names, scorer=scorer, processor=processor,
        score_cutoff=cutoff, limit=limit,
    )
    return [(round(float(score), 1), int(index)) for _choice, score, index in hits]
