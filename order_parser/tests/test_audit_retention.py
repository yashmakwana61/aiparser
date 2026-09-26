"""Phase 14 hardening: audit archive verification, day-file retention."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from order_parser.api import auth as auth_module
from order_parser.api import monitoring as monitoring_module
from order_parser.api.monitoring import router as monitoring_router
from order_parser.core import metrics
from order_parser.core.audit import (
    prune_audit_days,
    verify_audit_directory,
    write_audit_entry,
)
from order_parser.core.metrics import REGISTRY
from order_parser.core.retention import run_retention_sweep
from order_parser.sessions.store import SessionStore


NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def entry_for(day: str, order_id: str) -> dict:
    """Pre-stamped timestamp so the entry lands in a chosen daily file."""
    return {
        "order_id": order_id,
        "status": "success",
        "timestamp": f"{day}T10:00:00+00:00",
    }


@pytest.fixture(autouse=True)
def _isolate():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


# ------------------------------------------------------------ verify archive


@pytest.mark.usefixtures("_isolate")
def test_verify_reports_intact_archive(tmp_path):
    write_audit_entry(entry_for("2026-08-21", "ord1"), directory=tmp_path)
    write_audit_entry(entry_for("2026-08-22", "ord2"), directory=tmp_path)

    report = verify_audit_directory(tmp_path)

    assert report == {
        "integrity_ok": True,
        "files_checked": 2,
        "breaks": {},
    }


@pytest.mark.usefixtures("_isolate")
def test_verify_detects_tampered_line_in_one_day_only(tmp_path):
    write_audit_entry(entry_for("2026-08-20", "ordA"), directory=tmp_path)
    write_audit_entry(entry_for("2026-08-21", "ordB"), directory=tmp_path)
    victim = tmp_path / "2026-08-20.jsonl"
    lines = victim.read_text(encoding="utf-8").splitlines()
    tampered = lines[0].replace("ordA", "ordHACKED")
    assert tampered != lines[0]
    victim.write_text("\n".join([tampered] + lines[1:]) + "\n", encoding="utf-8")

    report = verify_audit_directory(tmp_path)

    assert report["integrity_ok"] is False
    assert report["files_checked"] == 2
    assert list(report["breaks"].keys()) == ["2026-08-20.jsonl"]
    assert report["breaks"]["2026-08-20.jsonl"]


@pytest.mark.usefixtures("_isolate")
def test_verify_ignores_non_jsonl_files_and_empty_dirs(tmp_path):
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()

    for target in (tmp_path, empty):
        report = verify_audit_directory(target)
        assert report["integrity_ok"] is True
        assert report["files_checked"] == 0


@pytest.mark.usefixtures("_isolate")
def test_legacy_hashless_entries_do_not_break_archive(tmp_path):
    legacy_file = tmp_path / "2026-07-01.jsonl"
    legacy_file.write_text(
        '{"order_id": "old", "status": "success"}\n', encoding="utf-8"
    )
    write_audit_entry(entry_for("2026-08-01", "new"), directory=tmp_path)

    report = verify_audit_directory(tmp_path)

    assert report["integrity_ok"] is True
    assert report["files_checked"] == 2


# ------------------------------------------------------------- day pruning


@pytest.mark.usefixtures("_isolate")
def test_prune_removes_only_days_strictly_older_than_cutoff(tmp_path):
    for day in ("2026-06-30", "2026-07-24", "2026-08-23"):
        write_audit_entry(entry_for(day, "x"), directory=tmp_path)

    removed = prune_audit_days(30, directory=tmp_path, now=NOW)

    # cutoff = 2026-07-24; boundary day itself is kept.
    assert removed == 1
    names = {p.name for p in tmp_path.glob("*.jsonl")}
    assert names == {"2026-07-24.jsonl", "2026-08-23.jsonl"}
    assert REGISTRY.counter_value("retention_audit_files_pruned_total") == 1


@pytest.mark.usefixtures("_isolate")
def test_prune_skips_unparseable_names_and_disabled_setting(tmp_path):
    (tmp_path / "notes.jsonl").write_text("{}\n", encoding="utf-8")
    write_audit_entry(entry_for("2026-01-01", "ancient"), directory=tmp_path)

    assert prune_audit_days(0, directory=tmp_path, now=NOW) == 0
    assert prune_audit_days(30, directory=tmp_path, now=NOW) == 1

    remaining = {p.name for p in tmp_path.glob("*.jsonl")}
    assert remaining == {"notes.jsonl"}


@pytest.mark.usefixtures("_isolate")
def test_prune_missing_directory_is_noop(tmp_path):
    assert prune_audit_days(30, directory=tmp_path / "nope", now=NOW) == 0


# ---------------------------------------------------------- sweep integration


class FakeIdempotency:
    def purge(self, older_than_days: int) -> int:
        return 3


@pytest.mark.usefixtures("_isolate")
def test_sweep_includes_audit_pruning_when_enabled(tmp_path):
    store_dir = tmp_path / "sessions"
    audit_dir = tmp_path / "audit"
    store = SessionStore(store_dir)
    write_audit_entry(entry_for("2026-05-01", "old-order"), directory=audit_dir)

    result = run_retention_sweep(
        store,
        FakeIdempotency(),
        retention_days=30,
        now=NOW,
        audit_retention_days=30,
        audit_directory=str(audit_dir),
    )

    assert result.audit_files_pruned == 1
    assert result.idempotency_rows_purged == 3
    assert not (audit_dir / "2026-05-01.jsonl").exists()


@pytest.mark.usefixtures("_isolate")
def test_sweep_keeps_audit_forever_by_default(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    audit_dir = tmp_path / "audit"
    write_audit_entry(entry_for("2025-01-01", "historic"), directory=audit_dir)

    result = run_retention_sweep(store, None, retention_days=30, now=NOW)

    assert result.audit_files_pruned == 0
    assert (audit_dir / "2025-01-01.jsonl").exists()


# ------------------------------------------------------------------ endpoint


def build_client(monkeypatch) -> TestClient:
    class NoAuth:
        api_auth_token = ""

    monkeypatch.setattr(auth_module, "get_settings", lambda: NoAuth())
    app = FastAPI()
    app.include_router(monitoring_router)
    return TestClient(app)


@pytest.mark.usefixtures("_isolate")
def test_audit_verify_endpoint_ok_and_gated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        monitoring_module, "verify_audit_directory", lambda: {"integrity_ok": True, "files_checked": 4, "breaks": {}}
    )
    client = build_client(monkeypatch)

    response = client.get("/audit/verify")

    assert response.status_code == 200
    assert response.json() == {"integrity_ok": True, "files_checked": 4, "breaks": {}}


@pytest.mark.usefixtures("_isolate")
def test_audit_verify_endpoint_fails_on_tamper(tmp_path, monkeypatch):
    monkeypatch.setattr(
        monitoring_module,
        "verify_audit_directory",
        lambda: {"integrity_ok": False, "files_checked": 1, "breaks": {"x.jsonl": ["line 1: content hash mismatch"]}},
    )
    client = build_client(monkeypatch)

    response = client.get("/audit/verify")

    assert response.status_code == 503
    assert response.json()["integrity_ok"] is False
