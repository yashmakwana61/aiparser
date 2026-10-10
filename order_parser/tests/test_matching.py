"""Central matching layer: scorer choice, cutoff enforcement, parity."""

from order_parser.resolution import matching
from order_parser.resolution.matching import (
    customer_scorer,
    legacy_product_scorer,
    normalize_name,
    sku_scorer,
    top_matches,
)


def test_customer_scorer_ignores_word_order():
    assert customer_scorer("Hotels Limited ITC", "ITC Hotels Limited") == 100.0
    assert customer_scorer("", "ITC Hotels Limited") == 0.0
    assert customer_scorer("ITC Hotels Limited", "") == 0.0


def test_sku_scorer_does_not_partially_inflate():
    assert sku_scorer("300233", "300233") == 100.0
    assert sku_scorer("30023", "300233") < 100.0
    assert sku_scorer("", "300233") == 0.0


def test_legacy_product_scorer_keeps_documented_calibration():
    # Values quoted in validators/product_validator.py.
    assert legacy_product_scorer("Dell Lattitude", "Dell Latitude") >= 95.0
    assert legacy_product_scorer("Keybord", "Keyboard") >= 90.0
    assert legacy_product_scorer("Lappy", "Laptop") >= 70.0
    assert legacy_product_scorer("Lappy", "Unrelated Thing XYZ") < 60.0


def test_top_matches_enforces_cutoff_limit_and_order():
    names = ["Alpha Beta", "Alpha Beta Gamma", "Completely Different", "Alpha"]
    hits = top_matches(names, "Alpha Beta", customer_scorer, 72.0, processor=normalize_name)
    assert hits, "exact hit must clear"
    assert hits[0] == (100.0, 0)
    assert all(score >= 72.0 for score, _ in hits)
    limited = top_matches(names, "Alpha", customer_scorer, 0.0, limit=2,
                          processor=normalize_name)
    assert len(limited) == 2
    scores = [score for score, _ in limited]
    assert scores == sorted(scores, reverse=True)


def test_top_matches_empty_inputs():
    assert top_matches([], "x", customer_scorer, 0.0) == []
    assert matching.top_matches(["a"], "", customer_scorer, 0.0) == []
