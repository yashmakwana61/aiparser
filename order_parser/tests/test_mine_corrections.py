"""Correction mining: repeated human fixes become alias suggestions."""

import json
from types import SimpleNamespace

from order_parser.tools.maintenance import cmd_mine_corrections


def _write_audit(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _correction(original, target, field="item.product_name"):
    return {
        "event": "user_action", "action": "correction_applied",
        "case_id": "ORD-1", "actor": "u",
        "detail": {"summary": "x",
                   "correction": {"field": field, "original_value": original,
                                  "corrected_value": "y", "target": target}},
    }


def test_mine_surfaces_repeated_corrections(tmp_path):
    audit = tmp_path / "audit" / "2026-10-07.jsonl"
    _write_audit(audit, [
        _correction("Lappy", {"product_id": 125}),
        _correction("Lappy", {"product_id": 125}),
        _correction("Lappy", {"product_id": 125}),
        _correction("Bred", {"product_id": 165}),
        _correction("Nope", {}),
        {"event": "audit.entry_written", "order_id": "x"},
    ])
    result = cmd_mine_corrections(SimpleNamespace(dir=str(tmp_path / "audit"), min_repeats=2))
    assert result["corrections_seen"] == 5
    assert result["without_usable_target"] == 1
    assert len(result["alias_candidates"]) == 1
    candidate = result["alias_candidates"][0]
    assert candidate["original_value"] == "Lappy"
    assert candidate["target_id"] == "125"
    assert candidate["times"] == 3


def test_mine_empty_archive(tmp_path):
    (tmp_path / "audit").mkdir()
    result = cmd_mine_corrections(SimpleNamespace(dir=str(tmp_path / "audit"), min_repeats=2))
    assert result == {"corrections_seen": 0, "without_usable_target": 0, "alias_candidates": []}
