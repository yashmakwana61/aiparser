"""Alias hygiene: dangling targets from catalog rebuilds are reported, never guessed."""

from order_parser.resolution.alias_store import AliasStore


def _seed(tmp_path):
    store = AliasStore(tmp_path / "aliases")
    live = store.create_product("Bread White 700g", 165, created_by="staff")
    dead = store.create_product("Old Widget", 999001, created_by="staff")
    gone = store.create_customer("Ghost Traders", 888001, created_by="staff")
    return store, live, dead, gone


def test_validate_targets_reports_dead_but_changes_nothing(tmp_path):
    store, live, dead, gone = _seed(tmp_path)
    report = store.validate_targets({165, 166}, lambda pid: pid != 888001)
    assert [d["id"] for d in report["product"]] == [dead.id]
    assert [d["id"] for d in report["customer"]] == [gone.id]
    # Report-only: everything stays active.
    assert store.find_product("bread white 700g") is not None
    assert store.find_product("old widget") is not None


def test_validate_targets_quiet_when_healthy(tmp_path):
    store, _live, _dead, _gone = _seed(tmp_path)
    report = store.validate_targets({165, 999001}, lambda pid: True)
    assert report == {"product": [], "customer": []}


def test_deactivate_removes_dead_alias_from_matching(tmp_path):
    store, _live, dead, _gone = _seed(tmp_path)
    assert store.deactivate("product", dead.id, deactivated_by="ops") is True
    assert store.find_product("old widget") is None
    assert store.deactivate("product", "no-such-id") is False


def test_validate_skips_checks_when_no_data(tmp_path):
    store, _live, _dead, _gone = _seed(tmp_path)
    assert store.validate_targets(None, None) == {"product": [], "customer": []}
