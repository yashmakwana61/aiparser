from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(value: str | None) -> str:
    """Deterministic text normalization: NFKC, casefold, punctuation to spaces."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = _NON_ALNUM.sub(" ", text)
    return " ".join(text.split()).strip()


def normalize_sku(value: str | None) -> str:
    """SKU normalization: uppercase alphanumerics only."""
    return re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKC", value or "").upper())


def sort_tokens(value: str | None) -> str:
    """Token-sorted normalized form (catches word-order differences)."""
    return " ".join(sorted(normalize_name(value).split()))


def normalized_variants(value: str | None) -> list[str]:
    """All normalized lookup keys for a name (base form plus token-sorted)."""
    variants: list[str] = []
    base = normalize_name(value)
    if base:
        variants.append(base)
    sorted_form = sort_tokens(value)
    if sorted_form and sorted_form not in variants:
        variants.append(sorted_form)
    return variants
