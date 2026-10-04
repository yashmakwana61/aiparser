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


# Common company-suffix abbreviations expanded before token comparison so
# "HOT CAKES PRIVATE LTD" matches a "HOT CAKES PRIVATE LIMITED" entry.
_COMPANY_ABBREVIATIONS = {
    "ltd": "limited",
    "pvt": "private",
    "co": "company",
    "corp": "corporation",
    "inc": "incorporated",
    "llp": "limited liability partnership",
    "llc": "limited liability company",
    "mfg": "manufacturing",
    "ent": "enterprises",
    "ents": "enterprises",
    "intl": "international",
    "gen": "general",
    "assoc": "associates",
    "bros": "brothers",
}


def company_tokens(value: str | None) -> set[str]:
    """Normalized significant tokens for company-name comparison."""
    tokens = set()
    for token in normalize_name(value).split():
        tokens.add(_COMPANY_ABBREVIATIONS.get(token, token))
    # Multi-word expansions (e.g. "llp") contribute their parts too.
    expanded = set()
    for token in tokens:
        expanded.update(token.split())
    return expanded


def matches_never_customer(name: str | None, raw_entries: str | None) -> str | None:
    """Return the matching never-customer entry, or None.

    An entry matches when its significant tokens are all present in the
    name (abbreviation-tolerant: LTD ~ LIMITED, PVT ~ PRIVATE) or when
    the raw normalized entry is a substring of the normalized name.
    """
    normalized = normalize_name(name)
    if not normalized:
        return None
    name_tokens = company_tokens(name)
    for raw in (raw_entries or "").split(","):
        entry = raw.strip()
        if not entry:
            continue
        norm_entry = normalize_name(entry)
        if norm_entry and norm_entry in normalized:
            return entry
        entry_tokens = company_tokens(entry)
        if entry_tokens and entry_tokens <= name_tokens:
            return entry
    return None
