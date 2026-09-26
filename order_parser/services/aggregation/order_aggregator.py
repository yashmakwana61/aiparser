from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.services.aggregation.conflict_engine import Conflict, ConflictEngine
from order_parser.services.aggregation.merge_strategy import (
    FieldCandidate,
    FieldDecision,
    MergeStrategy,
    norm_text,
)

logger = structlog.get_logger(__name__)

ITEM_FIELDS = ("quantity", "uom", "unit_price")


@dataclass
class AggregationEntry:
    """One extracted fragment offered to the aggregator."""

    label: str  # "text#1", "excel:orders.xlsx", "caption:order.jpg", ...
    parsed: ParsedOrder
    kind: str = ""  # derived from the label prefix when empty
    timestamp: str = ""
    confidence: float | None = None


@dataclass
class ItemAggregate:
    product_name: FieldDecision
    quantity: FieldDecision
    uom: FieldDecision
    unit_price: FieldDecision


@dataclass
class AggregationResult:
    parsed: ParsedOrder
    customer_fields: dict[str, FieldDecision] = field(default_factory=dict)
    items: list[ItemAggregate] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    forced_confirmation: bool = False

    @property
    def conflict_messages(self) -> list[str]:
        return [c.message for c in self.conflicts]

    def provenance(self) -> dict[str, Any]:
        return {
            "customer": {
                name: {"rule": d.rule, "source": d.chosen.source if d.chosen else None}
                for name, d in self.customer_fields.items()
            },
            "items": [
                {
                    "product": agg.product_name.value,
                    **{
                        name: {"rule": getattr(agg, name).rule, "conflict": getattr(agg, name).conflict}
                        for name in ITEM_FIELDS
                    },
                }
                for agg in self.items
            ],
            "conflicts": [c.as_dict() for c in self.conflicts],
        }


class OrderAggregator:
    """Merges fragment orders deterministically with full provenance.

    Merge policy (documented, never arbitrary):
    - customer fields: candidates ranked text > excel > pdf > image > caption,
      then confidence, then earliest arrival. Compatible name variants pick
      the longest form; genuinely different names are a material conflict.
    - items: grouped by normalized product name; identical restatements
      collapse; divergent quantity/UOM/price keep the highest-priority value
      and raise material conflicts that force staff confirmation.
    - missing scalar values are enriched from lower-priority fragments.
    """

    def __init__(self) -> None:
        self.engine = ConflictEngine()

    # ------------------------------------------------------------------ public

    def aggregate(self, entries: list[AggregationEntry]) -> AggregationResult:
        normalized: list[AggregationEntry] = []
        for position, entry in enumerate(entries):
            kind = entry.kind or entry.label.split(":", 1)[0].split("#", 1)[0]
            confidence = (
                entry.confidence
                if entry.confidence is not None
                else float(entry.parsed.order.metadata.confidence or 0)
            )
            normalized.append(
                AggregationEntry(
                    label=entry.label,
                    parsed=entry.parsed,
                    kind=kind,
                    timestamp=entry.timestamp,
                    confidence=confidence,
                )
            )

        customer_fields, customer_candidates = self._merge_customer(normalized)
        item_aggregates, item_decisions = self._merge_items(normalized)

        conflicts: list[Conflict] = []
        customer_conflict = self.engine.customer_conflict(customer_fields.get("name", FieldDecision("name", None, "missing")))
        if customer_conflict:
            conflicts.append(customer_conflict)
        for aggregate in item_aggregates:
            conflicts.extend(
                self.engine.item_conflicts(
                    str(aggregate.product_name.value),
                    {
                        "quantity": aggregate.quantity,
                        "uom": aggregate.uom,
                        "unit_price": aggregate.unit_price,
                    },
                )
            )

        notes = [f"aggregated from {len(normalized)} source(s)"]
        parsed = self._assemble(normalized, customer_fields, item_aggregates, notes)

        material = any(c.material for c in conflicts)
        result = AggregationResult(
            parsed=parsed,
            customer_fields=customer_fields,
            items=item_aggregates,
            conflicts=conflicts,
            notes=notes,
            forced_confirmation=material,
        )
        logger.info(
            "aggregation.completed",
            fragments=len(normalized),
            items=len(item_aggregates),
            conflicts=len(conflicts),
            forced_confirmation=material,
        )
        return result

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _candidate(source_label: str, kind: str, confidence: float, timestamp: str, position: int, value: Any) -> FieldCandidate:
        return FieldCandidate(
            value=value,
            source=source_label,
            kind=kind,
            confidence=confidence,
            timestamp=timestamp,
            position=position,
        )

    def _merge_customer(
        self, entries: list[AggregationEntry]
    ) -> tuple[dict[str, FieldDecision], dict[str, list[FieldCandidate]]]:
        fields = ("name", "email", "phone")
        candidates: dict[str, list[FieldCandidate]] = {name: [] for name in fields}
        for position, entry in enumerate(entries):
            customer = entry.parsed.order.customer
            for name in fields:
                value = getattr(customer, name)
                if isinstance(value, str) and not value.strip():
                    continue
                candidates[name].append(
                    self._candidate(entry.label, entry.kind, entry.confidence or 0.0, entry.timestamp, position, value)
                )
        decisions = {name: MergeStrategy.select(name, values) for name, values in candidates.items()}

        # compatible name refinement: prefer the longest variant explicitly
        name_decision = decisions["name"]
        if name_decision.conflict and len(name_decision.others) == 1:
            longest = max([name_decision.chosen, *name_decision.others], key=lambda c: len(str(c.value or "")))
            if longest is not name_decision.chosen:
                name_decision = FieldDecision(
                    field="name",
                    value=longest.value,
                    rule="longest_variant",
                    chosen=longest,
                    others=[c for c in [name_decision.chosen, *name_decision.others] if c is not longest],
                    conflict=True,  # still surfaced; engine decides whether it is material
                )
                decisions["name"] = name_decision
        return decisions, candidates

    @staticmethod
    def _item_key(name: str) -> str:
        return norm_text(name)

    def _merge_items(
        self, entries: list[AggregationEntry]
    ) -> tuple[list[ItemAggregate], dict[str, dict[str, FieldDecision]]]:
        order_of_keys: list[str] = []
        product_candidates: dict[str, list[FieldCandidate]] = {}
        field_candidates: dict[str, dict[str, list[FieldCandidate]]] = {}

        for position, entry in enumerate(entries):
            for item in entry.parsed.order.items:
                key = self._item_key(item.product_name)
                if not key:
                    continue
                if key not in field_candidates:
                    order_of_keys.append(key)
                    product_candidates[key] = []
                    field_candidates[key] = {name: [] for name in ITEM_FIELDS}
                product_candidates[key].append(
                    self._candidate(entry.label, entry.kind, entry.confidence or 0.0, entry.timestamp, position, item.product_name)
                )
                for name in ITEM_FIELDS:
                    value = getattr(item, name)
                    if name == "unit_price" and value is None:
                        continue  # absence is not a conflicting value
                    if name == "quantity" and value in (None, 0):
                        continue
                    if name == "uom" and not str(value or "").strip():
                        continue
                    field_candidates[key][name].append(
                        self._candidate(entry.label, entry.kind, entry.confidence or 0.0, entry.timestamp, position, value)
                    )

        aggregates: list[ItemAggregate] = []
        all_decisions: dict[str, dict[str, FieldDecision]] = {}
        for key in order_of_keys:
            product_decision = MergeStrategy.select("product_name", product_candidates[key])
            display = max((c.value for c in product_candidates[key]), key=lambda v: len(str(v or "")))
            decisions = {name: MergeStrategy.select(name, field_candidates[key][name]) for name in ITEM_FIELDS}

            product_display = FieldDecision(
                field="product_name",
                value=display,
                rule=product_decision.rule,
                chosen=product_decision.chosen,
            )
            aggregates.append(
                ItemAggregate(
                    product_name=product_display,
                    quantity=decisions["quantity"],
                    uom=decisions["uom"],
                    unit_price=decisions["unit_price"],
                )
            )
            all_decisions[key] = {**decisions, "product_name": product_display}
        return aggregates, all_decisions

    def _assemble(
        self,
        entries: list[AggregationEntry],
        customer_fields: dict[str, FieldDecision],
        aggregates: list[ItemAggregate],
        notes: list[str],
    ) -> ParsedOrder:
        customer = CustomerModel(
            name=str(customer_fields["name"].value or ""),
            email=str(customer_fields["email"].value or "") if "email" in customer_fields else "",
            phone=str(customer_fields["phone"].value or "") if "phone" in customer_fields else "",
        )
        for name in ("email", "phone"):
            if not getattr(customer, name):
                filled, _ = MergeStrategy.fill_missing(None, self._all_candidates(entries, name))
                if filled:
                    setattr(customer, name, str(filled))

        items: list[ItemModel] = []
        for aggregate in aggregates:
            items.append(
                ItemModel(
                    product_name=str(aggregate.product_name.value or ""),
                    quantity=float(aggregate.quantity.value or 0) if aggregate.quantity.value is not None else 0,
                    uom=str(aggregate.uom.value or "Units") if aggregate.uom.value is not None else "Units",
                    unit_price=float(aggregate.unit_price.value) if aggregate.unit_price.value is not None else None,
                )
            )

        confidences = [
            float(e.parsed.order.metadata.confidence or 0) for e in entries if e.parsed.order.metadata.confidence
        ]
        merged_notes = "; ".join(notes + [e.parsed.order.metadata.notes for e in entries if e.parsed.order.metadata.notes])
        parsed = ParsedOrder(
            order=OrderModel(
                customer=customer,
                items=items,
                metadata=MetadataModel(
                    source="telegram",
                    input_type="session",
                    confidence=max(confidences) if confidences else 0.0,
                    notes=merged_notes[:4000],
                ),
            ),
            extracted_text="\n---\n".join(e.parsed.extracted_text for e in entries if e.parsed.extracted_text)[:20000],
        )
        return parsed

    @staticmethod
    def _all_candidates(entries: list[AggregationEntry], field_name: str) -> list[FieldCandidate]:
        candidates: list[FieldCandidate] = []
        for position, entry in enumerate(entries):
            value = getattr(entry.parsed.order.customer, field_name, "")
            if isinstance(value, str) and value.strip():
                candidates.append(FieldCandidate(value=value, source=entry.label, kind=entry.kind, position=position))
        return candidates
