from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def norm_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


@dataclass
class FieldCandidate:
    """One observed value for a field, with full provenance."""

    value: Any
    source: str  # fragment label, e.g. "text#1" or "excel:orders.xlsx"
    kind: str  # text | excel | pdf | image | caption
    confidence: float = 0.0
    timestamp: str = ""
    position: int = 0


@dataclass
class FieldDecision:
    """Deterministic outcome of merging all candidates for one field."""

    field: str
    value: Any
    rule: str  # missing | only_source | identical_restatement | priority_wins | enrichment_fill | longest_variant
    chosen: FieldCandidate | None = None
    others: list[FieldCandidate] = field(default_factory=list)
    conflict: bool = False


# Channel reliability ranking for scalar fields. Higher wins; ties break by
# confidence, then by earliest arrival. This ordering is documented policy -
# never an arbitrary runtime choice - and every winning candidate is recorded.
KIND_PRIORITY: dict[str, int] = {
    "text": 5,  # deliberately typed by staff
    "excel": 4,  # deterministic structured extraction
    "pdf": 3,  # machine-generated document text
    "image": 2,  # OCR + AI interpretation
    "caption": 1,  # incidental annotation
}


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
        tolerance = 0.01 if isinstance(a, float) or isinstance(b, float) else 1e-9
        try:
            return abs(float(a) - float(b)) <= max(tolerance, 1e-9)
        except (TypeError, ValueError):
            return False
    return norm_text(a) == norm_text(b)


class MergeStrategy:
    """Applies the documented precedence rules to candidate values."""

    @staticmethod
    def select(field_name: str, candidates: list[FieldCandidate]) -> FieldDecision:
        if not candidates:
            return FieldDecision(field=field_name, value=None, rule="missing")
        if len(candidates) == 1:
            return FieldDecision(
                field=field_name, value=candidates[0].value, rule="only_source", chosen=candidates[0]
            )

        distinct: list[FieldCandidate] = []
        for candidate in candidates:
            if not any(_values_equal(candidate.value, kept.value) for kept in distinct):
                distinct.append(candidate)

        if len(distinct) == 1:
            chosen = min(candidates, key=lambda c: c.position)
            return FieldDecision(
                field=field_name,
                value=chosen.value,
                rule="identical_restatement",
                chosen=chosen,
                others=[c for c in candidates if c is not chosen],
            )

        ranked = sorted(candidates, key=lambda c: (-KIND_PRIORITY.get(c.kind, 0), -c.confidence, c.position))
        chosen = ranked[0]
        return FieldDecision(
            field=field_name,
            value=chosen.value,
            rule="priority_wins",
            chosen=chosen,
            others=[c for c in distinct if c is not chosen],
            conflict=True,
        )

    @staticmethod
    def fill_missing(target: Any, candidates: list[FieldCandidate]) -> tuple[Any, FieldCandidate | None]:
        """First non-empty candidate value for an empty target (enrichment)."""
        for candidate in candidates:
            value = candidate.value
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value, candidate
        return target, None
