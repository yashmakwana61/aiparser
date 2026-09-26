import json
from pathlib import Path

import pytest

from order_parser.core.audit import write_audit_entry
from order_parser.core.pending_store import PendingStore
from order_parser.sessions.store import SessionStore
from order_parser.tools import maintenance


def _make_session(session_id: str, status: str, updated_iso: str):
    from order_parser.sessions.models import StaffSession

    return StaffSession(
        session_id=session_id,
        staff_id="bob",
        staff_name="Bob",
        telegram_user_id=111,
        chat_id=555,
        status=status,
        created_at=updated_iso,
        updated_at=updated_iso,
    )


# ------------------------------------------------------------------ helpers


@pytest.fixture
def audit_dir(tmp_path):
    directory = tmp_path / "audit"
    directory.mkdir()
    for i in range(3):
        write_audit_entry({"event": f"e{i}"}, directory=directory)
    return directory


def run_cli(argv):
    return maintenance.main(argv)


# ------------------------------------------------------------- verify-audit


def test_verify_audit_intact_exits_zero(audit_dir, capsys):
    code = run_cli(["verify-audit", "--dir", str(audit_dir)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["integrity_ok"] is True
    assert payload["files_checked"] == 1
    assert payload["breaks"] == {}


def test_verify_audit_tampered_exits_one(audit_dir, capsys):
    day_file = next(audit_dir.glob("*.jsonl"))
    lines = day_file.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["event"] = "tampered"
    lines[1] = json.dumps(record)
    day_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    code = run_cli(["verify-audit", "--dir", str(audit_dir)])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["integrity_ok"] is False
    assert day_file.name in payload["breaks"]


# ----------------------------------------------------------- list-quarantine


def test_list_quarantine_reports_corrupt_files(tmp_path, capsys):
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "a.json.corrupt-xyz").write_text("{broken")
    (tmp_path / "pending").mkdir()
    (tmp_path / "pending" / "b.json.corrupt-abc").write_text("nope")

    code = run_cli(["list-quarantine", "--dir", str(tmp_path)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    assert payload["total_bytes"] > 0
    names = [entry["path"] for entry in payload["files"]]
    assert any("a.json.corrupt-" in n for n in names)


def test_list_quarantine_empty_when_clean(tmp_path, capsys):
    code = run_cli(["list-quarantine", "--dir", str(tmp_path)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"count": 0, "total_bytes": 0, "files": []}


# ------------------------------------------------------------ pending-summary


def test_pending_summary_counts_by_status(tmp_path, capsys):
    store = PendingStore(tmp_path / "pending")
    store.save({"order_id": "ORD-1", "status": "review"})
    store.save({"order_id": "ORD-2", "status": "review"})
    store.save({"order_id": "ORD-3", "status": "confirmed"})

    code = run_cli(["pending-summary", "--dir", str(store.directory)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"count": 3, "by_status": {"review": 2, "confirmed": 1}}


# ------------------------------------------------------------- prune-sessions


def test_prune_sessions_removes_only_old_terminal(tmp_path, capsys):
    store = SessionStore(tmp_path / "sessions")
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=40)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()

    store.save(_make_session("old-done", "COMPLETED", old))
    store.save(_make_session("old-active", "COLLECTING", old))
    store.save(_make_session("recent-done", "COMPLETED", recent))

    code = run_cli(["prune-sessions", "--days", "30", "--store-dir", str(store.directory)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions_pruned"] == 1
    assert store.get("old-active") is not None
    assert store.get("recent-done") is not None
    assert store.get("old-done") is None


# ------------------------------------------------------------------ usage


def test_unknown_command_is_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        run_cli(["no-such-command"])
    assert excinfo.value.code == 2


# ------------------------------------------------------- prune-sessions --dry-run


def test_prune_dry_run_reports_without_deleting(tmp_path, capsys):
    from datetime import datetime, timedelta, timezone
    from order_parser.sessions.models import StaffSession

    store = SessionStore(tmp_path / "sessions")
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=40)).isoformat()
    stale = StaffSession(
        session_id="ses_old01",
        staff_id="bob",
        staff_name="Bob",
        telegram_user_id=111,
        chat_id=555,
        status="COMPLETED",
        created_at=old,
        updated_at=old,
    )
    store.save(stale)

    code = run_cli([
        "prune-sessions", "--days", "30", "--store-dir", str(store.directory), "--dry-run",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert [s["session_id"] for s in payload["sessions"]] == ["ses_old01"]
    assert payload["session_count"] == 1
    assert store.get("ses_old01") is not None


def test_export_pending_writes_jsonl_snapshot(tmp_path, capsys):
    store = PendingStore(tmp_path / "pending")
    store.save({"order_id": "ORD-1", "status": "review", "customer": "Acme"})
    store.save({"order_id": "ORD-2", "status": "confirmed", "customer": "Beta"})
    out_file = tmp_path / "snapshot.jsonl"

    code = run_cli([
        "export-pending", "--dir", str(store.directory),
        "--status", "review", "--out", str(out_file),
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["path"] == str(out_file)

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["order_id"] == "ORD-1"


def test_export_pending_defaults_to_timestamped_sibling(tmp_path, capsys):
    store = PendingStore(tmp_path / "pending")
    store.save({"order_id": "ORD-9", "status": "review"})

    code = run_cli(["export-pending", "--dir", str(store.directory)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    exported = Path(payload["path"])
    assert exported.exists()
    assert exported.parent == tmp_path
    assert exported.name.startswith("pending-export-")
