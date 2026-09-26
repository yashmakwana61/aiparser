"""Order aggregation (Phase 5).

Merges overlapping fragments (text, image, PDF, Excel) of one staff session
into a single order with full field provenance. Values are never blindly
overwritten and conflicts are never silently resolved: every decision records
source / confidence / timestamp / priority / rule, material conflicts force
the confirmation flow.
"""

from order_parser.services.aggregation.conflict_engine import Conflict, ConflictEngine
from order_parser.services.aggregation.merge_strategy import (
    FieldCandidate,
    FieldDecision,
    MergeStrategy,
)
from order_parser.services.aggregation.order_aggregator import (
    AggregationEntry,
    AggregationResult,
    OrderAggregator,
)

__all__ = [
    "Conflict",
    "ConflictEngine",
    "FieldCandidate",
    "FieldDecision",
    "MergeStrategy",
    "AggregationEntry",
    "AggregationResult",
    "OrderAggregator",
]
