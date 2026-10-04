"""Durable job-id sequencing: restarts must never reuse an ORD-... id."""

import json
import re
from datetime import datetime, timezone

from order_parser.core.job_runner import create_job
from order_parser.core.job_store import JobStore

ID_RE = re.compile(r"^ORD-\d{8}-\d{6}$")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def test_ids_are_sequential_and_formatted(tmp_path):
    store = JobStore(tmp_path / "jobs")
    first, second = store.next_job_id(), store.next_job_id()
    assert ID_RE.match(first) and ID_RE.match(second)
    assert first != second
    assert int(first.rsplit("-", 1)[1]) + 1 == int(second.rsplit("-", 1)[1])


def test_sequence_survives_restart(tmp_path):
    first_store = JobStore(tmp_path / "jobs")
    taken = {first_store.next_job_id() for _ in range(3)}
    # Simulate a process restart: brand-new store over the same directory.
    restarted = JobStore(tmp_path / "jobs")
    assert restarted.next_job_id() not in taken


def test_sequence_seeds_from_existing_job_files(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / f"ORD-{_today()}-000007.json").write_text(json.dumps({"job_id": "x"}), encoding="utf-8")
    store = JobStore(jobs_dir)
    assert store.next_job_id() == f"ORD-{_today()}-000008"


def test_corrupt_counter_recovers_from_scan(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job_a, _ = create_job(store, source="api", input_type="text", input_hash="aaa")
    job_b, _ = create_job(store, source="api", input_type="text", input_hash="bbb")
    assert job_a.job_id != job_b.job_id
    (tmp_path / "jobs" / f".sequence-{_today()}").write_text("garbage!!", encoding="utf-8")
    recovered, _ = create_job(store, source="api", input_type="text", input_hash="ccc")
    assert recovered.job_id == f"ORD-{_today()}-000003"


def test_create_job_never_overwrites_across_restart(tmp_path):
    jobs_dir = tmp_path / "jobs"
    job_a, _ = create_job(JobStore(jobs_dir), source="api", input_type="text", input_hash="aaa")
    restarted_store = JobStore(jobs_dir)
    job_b, _ = create_job(restarted_store, source="api", input_type="text", input_hash="bbb")
    assert job_a.job_id != job_b.job_id
    # Both job files exist with their own content — no clobbering.
    raw_a = json.loads((jobs_dir / f"{job_a.job_id}.json").read_text(encoding="utf-8"))
    raw_b = json.loads((jobs_dir / f"{job_b.job_id}.json").read_text(encoding="utf-8"))
    assert raw_a["input_hash"] == "aaa"
    assert raw_b["input_hash"] == "bbb"
