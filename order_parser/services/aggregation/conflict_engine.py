from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from order_parser.services.aggregation.merge_strategy import FieldDecision


@dataclass
class Conflict:
    code: str  # QUANTITY_CONFLICT | UOM_CONFLICT | PRICE_CONFLICT | CUSTOMER_CONFLICT
    subject: str  # product name or "customer"
    message: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    material: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "subject": self.subject,
            "message": self.message,
            "sources": self.sources,
            "material": self.material,
        }


class ConflictEngine:
    """Classifies divergent candidate values.

    Material conflicts (quantity / UOM / price / genuinely different
    customers) must be confirmed by staff. Compatible customer-name variants
    ("ABC Industries" vs "ABC Industries Pvt Ltd") are informational only -
    the master-data resolver decides identity against Odoo anyway.
    """

    @staticmethod
    def from_decision(code: str, subject: str, decision: FieldDecision, material: bool = True) -> Conflict | None:
        if not decision.conflict or decision.chosen is None:
            return None
        sources = [
            {"source": c.source, "value": c.value}
            for c in ([decision.chosen] + list(decision.others))
        ]
        return Conflict(
            code=code,
            subject=subject,
            message=(
                f"{code.replace('_', ' ').title()} for {subject}: "
                + " vs ".join(f"{s['source']}={s['value']}" for s in sources)
            ),
            sources=sources,
            material=material,
        )

    @staticmethod
    def customer_conflict(decision: FieldDecision) -> Conflict | None:
        """Different names are material; compatible refinements are not."""
        if not decision.conflict or decision.chosen is None or len(decision.others) == 0:
            return None
        chosen_tokens = set(str(decision.value or "").casefold().split())
        compatible = all(
            set(str(other.value or "").casefold().split()) <= chosen_tokens
            or chosen_tokens <= set(str(other.value or "").casefold().split())
            for other in decision.others
        )
        if compatible:
            return None
        return ConflictEngine.from_decision("CUSTOMER_CONFLICT", "customer", decision, material=True)

    @staticmethod
    def item_conflicts(product_name: str, decisions: dict[str, FieldDecision]) -> list[Conflict]:
        conflicts: list[Conflict] = []
        mapping = {
            "quantity": "QUANTITY_CONFLICT",
            "uom": "UOM_CONFLICT",
            "unit_price": "PRICE_CONFLICT",
        }
        for field_name, code in mapping.items():
            conflict = ConflictEngine.from_decision(code, f"'{product_name}'", decisions.get(field_name))
            if conflict:
                conflicts.append(conflict)
        return conflicts
