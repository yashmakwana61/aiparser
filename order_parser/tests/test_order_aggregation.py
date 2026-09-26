"""Phase 5: order aggregation + conflict engine."""
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.services.aggregation import (
    AggregationEntry,
    ConflictEngine,
    FieldCandidate,
    MergeStrategy,
    OrderAggregator,
)


def parsed(customer="", items=None, confidence=90.0, extracted=""):
    return ParsedOrder(
        order=OrderModel(
            customer=CustomerModel(name=customer),
            items=[ItemModel(product_name=n, quantity=q, uom=u, unit_price=p) for n, q, u, p in (items or [])],
            metadata=MetadataModel(source="telegram", input_type="text", confidence=confidence),
        ),
        extracted_text=extracted,
    )


def entry(label, order, kind=None):
    return AggregationEntry(label=label, parsed=order, kind=kind or "")


# ------------------------------------------------------------ merge strategy


def test_single_candidate_is_only_source():
    decision = MergeStrategy.select("quantity", [FieldCandidate(value=20, source="text#1", kind="text")])
    assert decision.rule == "only_source" and decision.value == 20 and not decision.conflict


def test_identical_restatements_collapse_to_earliest():
    candidates = [
        FieldCandidate(value=20, source="text#1", kind="text", position=0),
        FieldCandidate(value=20.0, source="excel:o.xlsx", kind="excel", confidence=100.0, position=1),
    ]
    decision = MergeStrategy.select("quantity", candidates)
    assert decision.rule == "identical_restatement"
    assert decision.chosen.source == "text#1" and len(decision.others) == 1
    assert not decision.conflict


def test_conflicting_values_flag_conflict_and_rank_by_priority():
    candidates = [
        FieldCandidate(value=25, source="excel:o.xlsx", kind="excel", confidence=100.0, position=1),
        FieldCandidate(value=20, source="text#1", kind="text", position=0),
    ]
    decision = MergeStrategy.select("quantity", candidates)
    assert decision.conflict is True and decision.rule == "priority_wins"
    assert decision.value == 20 and decision.chosen.source == "text#1"  # text outranks excel


def test_price_enrichment_fills_missing_without_conflict():
    from order_parser.services.aggregation.merge_strategy import norm_text  # noqa

    candidates = [
        FieldCandidate(value=450.0, source="pdf:o.pdf", kind="pdf", position=1),
        FieldCandidate(value=460.0, source="text#2", kind="text", position=2),
    ]
    filled, filler = MergeStrategy.fill_missing(None, candidates)
    assert filled == 450.0 and filler.source == "pdf:o.pdf"


# ----------------------------------------------------------- conflict engine


def test_customer_compatible_variants_are_not_material():
    decision = MergeStrategy.select(
        "name",
        [
            FieldCandidate(value="ABC Industries", source="text#1", kind="text", position=0),
            FieldCandidate(value="ABC Industries Pvt Ltd", source="pdf:o.pdf", kind="pdf", position=1),
        ],
    )
    assert ConflictEngine.customer_conflict(decision) is None  # token-subset refinement


def test_customer_genuinely_different_names_are_material():
    decision = MergeStrategy.select(
        "name",
        [
            FieldCandidate(value="ABC Industries", source="text#1", kind="text", position=0),
            FieldCandidate(value="Zeta Traders", source="image:o.jpg", kind="image", position=1),
        ],
    )
    conflict = ConflictEngine.customer_conflict(decision)
    assert conflict is not None and conflict.code == "CUSTOMER_CONFLICT" and conflict.material


def test_item_conflicts_cover_quantity_uom_price():
    decisions = {
        "quantity": MergeStrategy.select(
            "quantity",
            [FieldCandidate(value=20, source="a", kind="text"), FieldCandidate(value=25, source="b", kind="excel")],
        ),
        "uom": MergeStrategy.select("uom", [FieldCandidate(value="Units", source="a", kind="text")]),
        "unit_price": MergeStrategy.select(
            "unit_price",
            [FieldCandidate(value=450.0, source="a", kind="text"), FieldCandidate(value=480.0, source="b", kind="excel")],
        ),
    }
    conflicts = ConflictEngine.item_conflicts("'Bread'", decisions)
    codes = {c.code for c in conflicts}
    assert codes == {"QUANTITY_CONFLICT", "PRICE_CONFLICT"}
    quantity_conflict = next(c for c in conflicts if c.code == "QUANTITY_CONFLICT")
    assert {"source": "a", "value": 20} in quantity_conflict.sources


# --------------------------------------------------------------- aggregator


def test_aggregator_end_to_end_mixed_sources():
    aggregator = OrderAggregator()
    entries = [
        entry("text#1", parsed("ABC Industries", [("Bread White 400", 20, "Units", None)], 95), "text"),
        entry(
            "excel:orders.xlsx",
            parsed("", [("BREAD WHITE 400", 25, "Units", None), ("Milk 1L", 10, "Box", 800.0)], 100),
            "excel",
        ),
    ]
    result = aggregator.aggregate(entries)

    bread = result.items[0]
    assert str(bread.product_name.value).lower().startswith("bread")
    assert bread.quantity.conflict and bread.quantity.value == 20  # text wins, conflict recorded
    assert result.forced_confirmation is True
    codes = {c.code for c in result.conflicts}
    assert codes == {"QUANTITY_CONFLICT"}

    milk = next(i for i in result.items if "milk" in str(i.product_name.value).lower())
    assert float(milk.quantity.value) == 10 and float(milk.unit_price.value) == 800.0

    merged = result.parsed.order
    assert merged.customer.name == "ABC Industries"
    assert any(str(item.product_name).lower().startswith("bread") for item in merged.items)


def test_compatible_customer_variant_picks_longest_and_no_forced_confirm():
    aggregator = OrderAggregator()
    entries = [
        entry("text#1", parsed("ABC Industries", [("Bread", 5, "Units", None)], 95), "text"),
        entry("pdf:o.pdf", parsed("ABC Industries Pvt Ltd", [], 92), "pdf"),
    ]
    result = aggregator.aggregate(entries)
    assert result.parsed.order.customer.name == "ABC Industries Pvt Ltd"
    assert result.customer_fields["name"].rule == "longest_variant"
    assert result.forced_confirmation is False


def test_missing_price_filled_from_single_other_fragment():
    aggregator = OrderAggregator()
    entries = [
        entry("text#1", parsed("ABC", [("Bread", 20, "Units", None)], 95), "text"),
        entry("pdf:o.pdf", parsed("", [("bread", 20, "units", 450.0)], 90), "pdf"),
    ]
    result = aggregator.aggregate(entries)
    item = result.parsed.order.items[0]
    assert float(item.unit_price) == 450.0
    assert result.items[0].unit_price.rule == "only_source"  # sole price candidate
    assert result.forced_confirmation is False


def test_uom_conflict_detected_same_quantity():
    aggregator = OrderAggregator()
    entries = [
        entry("text#1", parsed("ABC", [("Bread", 20, "Units", None)], 95), "text"),
        entry("image:o.jpg", parsed("", [("bread", 20, "Box", None)], 85), "image"),
    ]
    result = aggregator.aggregate(entries)
    codes = {c.code for c in result.conflicts}
    assert "UOM_CONFLICT" in codes and result.forced_confirmation is True


def test_provenance_attached_to_merged_parsed_order():
    aggregator = OrderAggregator()
    result = aggregator.aggregate([entry("text#1", parsed("ABC", [("Bread", 3, "Units", None)]), "text")])
    provenance = result.provenance()
    assert provenance["items"][0]["product"] == "Bread"
    assert provenance["conflicts"] == []
