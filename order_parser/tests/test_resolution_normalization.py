from order_parser.resolution.normalization import (
    normalize_name,
    normalize_sku,
    normalized_variants,
    sort_tokens,
)


def test_normalize_name_strips_case_punctuation_and_whitespace():
    assert normalize_name("  White   BREAD, 400-Gms!! ") == "white bread 400 gms"


def test_normalize_name_handles_empty_values():
    assert normalize_name(None) == ""
    assert normalize_name("") == ""


def test_normalize_sku_keeps_alphanumerics_only():
    assert normalize_sku("bw-400.a") == "BW400A"
    assert normalize_sku(None) == ""


def test_sort_tokens_is_order_insensitive():
    assert sort_tokens("White Bread 400") == "400 bread white"
    assert sort_tokens("Bread White 400") == "400 bread white"


def test_normalized_variants_include_base_and_sorted_forms():
    variants = normalized_variants("Pro Keyboard!")
    assert variants[0] == "pro keyboard"
    assert "keyboard pro" in variants
