"""Callback protocol: compact encoding, strict server-side validation."""

import pytest

from order_parser.user_actions import callbacks as cb
from order_parser.user_actions.models import VERB_PICK_PRODUCT, VERBS


def test_encode_decode_roundtrip():
    data = cb.encode("ORD-20261004-000123", VERB_PICK_PRODUCT, 2, 1)
    parsed = cb.decode(data)
    assert parsed is not None
    assert (parsed.case_id, parsed.verb, parsed.item_index, parsed.candidate_ref) == (
        "ORD-20261004-000123", VERB_PICK_PRODUCT, 2, 1)


def test_encode_without_optional_parts():
    parsed = cb.decode(cb.encode("ORD-1", "st"))
    assert parsed is not None and parsed.item_index is None and parsed.candidate_ref is None


def test_encode_enforces_64_byte_limit():
    with pytest.raises(ValueError):
        cb.encode("ORD-" + "9" * 60, VERB_PICK_PRODUCT, 0, 0)


def test_decode_rejects_garbage():
    assert cb.decode("") is None
    assert cb.decode("sess:confirm") is None
    assert cb.decode("case:") is None
    assert cb.decode("case:ORD-1:nope") is None
    assert cb.decode("case:ORD-1:pr:x") is None
    assert cb.decode("case:ORD-1:pr:1:2:3") is None
    assert cb.decode("case::pr") is None


def test_all_verbs_decode():
    for verb in VERBS:
        assert cb.decode(f"case:ORD-1:{verb}") is not None


def test_ownership_forms():
    assert cb.owns_case(8751097833, "8751097833")
    assert cb.owns_case(8751097833, "user_8751097833")
    assert cb.owns_case("8751097833", "staff:8751097833")
    assert not cb.owns_case(111, "8751097833")
    assert not cb.owns_case(111, None)
    assert not cb.owns_case(None, "8751097833")
    # Different user, similar prefix must not match.
    assert not cb.owns_case(87510978, "8751097833")
