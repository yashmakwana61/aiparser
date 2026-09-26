"""Production hardening: outer job retries, queue backpressure, worker timeout.

Covers the incremental gaps closed for production-grade operation:
- run_job_sync retries TRANSIENT failures through RETRYING with backoff
- PERMANENT errors fail fast without retry
- POST /jobs and POST /parse return 429 (not silent drop) when the queue is full
- worker timeout marks the job FAILED/RESOURCE-001
- duplicate submissions return the existing job (no duplicate orders)
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from order_parser.api.jobs import parse_router, router as jobs_router
from order_parser.config import get_settings
from order_parser.core.errors import classify_error, is_retryable, ErrorCategory
from order_parser.core.job import JobRecord, JobStatus, can_transition
from order_parser.core.job_runner import (
    _retry_delay_seconds,
    create_job,
    run_job_sync,
)
from order_parser.core.job_store import JobStore


# ------------------------------------------------------------------ helpers


@pytest.fixture()
def settings_fast_retry(monkeypatch):
    """Zero backoff so retry tests run fast; restore after."""
    settings = get_settings()
    monkeypatch.setattr(settings, "job_max_retries", 3)
    monkeypatch.setattr(settings, "job_retry_backoff_seconds", 0.0)
    monkeypatch.setattr(settings, "job_retry_max_backoff_seconds", 0.0)
    monkeypatch.setattr(settings, "job_timeout_seconds", 300.0)
    return settings


def _store(tmp_path) -> JobStore:
    return JobStore(directory=str(tmp_path / "jobs"))


class _OkPipeline:
    def __init__(self, result=None):
        self.calls = 0
        self.result = result or {"status": "success", "confidence": 96.0, "customer": "ABC", "items": 1}

    def process(self, source, input_type, parsed, raw=None):
        self.calls += 1
        return dict(self.result)


class _FlakyPipeline:
    """Raises `failures` times, then succeeds."""

    def __init__(self, failures=2):
        self.calls = 0
        self.failures = failures

    def process(self, source, input_type, parsed, raw=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("AI API timeout")
        return {"status": "success", "confidence": 95.0, "customer": "ABC", "items": 2}


class _AlwaysFailPipeline:
    def __init__(self):
        self.calls = 0

    def process(self, source, input_type, parsed, raw=None):
        self.calls += 1
        raise RuntimeError("Odoo timeout")


class _PermanentPipeline:
    def __init__(self):
        self.calls = 0

    def process(self, source, input_type, parsed, raw=None):
        self.calls += 1
        exc = ValueError("order has no items")
        exc.error_code = "VALIDATION-001"
        raise exc


# ------------------------------------------------------------------ backoff


def test_retry_delay_is_exponential_and_capped():
    assert _retry_delay_seconds(0, 1.0, 30.0) == 1.0
    assert _retry_delay_seconds(1, 1.0, 30.0) == 2.0
    assert _retry_delay_seconds(2, 1.0, 30.0) == 4.0
    assert _retry_delay_seconds(10, 1.0, 30.0) == 30.0
    assert _retry_delay_seconds(0, 0.0, 0.0) == 0.0


def test_error_taxonomy_classification():
    assert classify_error("SYS-001") == ErrorCategory.TRANSIENT
    assert classify_error("ODOO-001") == ErrorCategory.TRANSIENT
    assert classify_error("OCR-001") == ErrorCategory.TRANSIENT
    assert classify_error("VALIDATION-001") == ErrorCategory.PERMANENT
    assert classify_error("RESOURCE-001") == ErrorCategory.PERMANENT
    assert classify_error("RES-001") == ErrorCategory.REVIEW_REQUIRED
    assert is_retryable("SYS-001") is True
    assert is_retryable("VALIDATION-001") is False
    assert is_retryable("RES-002") is False


# ------------------------------------------------------------------ retries


def test_transient_failure_retried_then_succeeds(tmp_path, settings_fast_retry):
    store = _store(tmp_path)
    job, dup = create_job(store, source="api", input_type="text", input_hash="retry-ok-1")
    assert dup is None
    pipeline = _FlakyPipeline(failures=2)
    result = run_job_sync(store, pipeline, job, parsed=object())
    assert result["status"] == "success"
    assert pipeline.calls == 3
    saved = store.get(job.job_id)
    assert saved.status == JobStatus.COMPLETED
    assert saved.retry_count == 2


def test_transient_failure_exhausted_marks_failed(tmp_path, settings_fast_retry):
    store = _store(tmp_path)
    job, _ = create_job(store, source="api", input_type="text", input_hash="retry-fail-1")
    pipeline = _AlwaysFailPipeline()
    result = run_job_sync(store, pipeline, job, parsed=object())
    assert result["status"] == "error"
    assert pipeline.calls == 4  # initial + 3 retries
    saved = store.get(job.job_id)
    assert saved.status == JobStatus.FAILED
    assert saved.error_code == "SYS-001"
    assert saved.retry_count == 3


def test_permanent_failure_not_retried(tmp_path, settings_fast_retry):
    store = _store(tmp_path)
    job, _ = create_job(store, source="api", input_type="text", input_hash="perm-1")
    pipeline = _PermanentPipeline()
    result = run_job_sync(store, pipeline, job, parsed=object())
    assert result["status"] == "error"
    assert pipeline.calls == 1
    saved = store.get(job.job_id)
    assert saved.status == JobStatus.FAILED
    assert saved.retry_count == 0


def test_invalid_state_transition_rejected():
    job = JobRecord(status=JobStatus.COMPLETED)
    with pytest.raises(ValueError):
        job.transition(JobStatus.PROCESSING)
    assert can_transition(JobStatus.FAILED, JobStatus.RETRYING) is True
    assert can_transition(JobStatus.RETRYING, JobStatus.QUEUED) is True
    assert can_transition(JobStatus.COMPLETED, JobStatus.QUEUED) is False


# ------------------------------------------------------- idempotency guard


def test_duplicate_input_hash_returns_existing_job(tmp_path, settings_fast_retry):
    store = _store(tmp_path)
    pipeline = _OkPipeline()
    job1, dup1 = create_job(store, source="api", input_type="text", input_hash="dup-1")
    assert dup1 is None
    run_job_sync(store, pipeline, job1, parsed=object())
    job2, dup2 = create_job(store, source="api", input_type="text", input_hash="dup-1")
    assert dup2 is not None
    assert job2.job_id == job1.job_id
    assert pipeline.calls == 1  # second submission never re-processed


# ------------------------------------------------------- queue backpressure


def _make_client(monkeypatch, tmp_path, queue):
    from unittest.mock import MagicMock

    from order_parser.api import auth as auth_module
    from order_parser.processors import text_processor as tp_module

    # Never hit the real AI gateway in API tests.
    monkeypatch.setattr(
        tp_module.TextProcessor, "process", lambda self, content: MagicMock(extracted_text=content)
    )
    # Disable API auth for these tests (same pattern as test_review_queue_api).
    class NoAuth:
        api_auth_token = ""

    monkeypatch.setattr(auth_module, "get_settings", lambda: NoAuth())

    app = FastAPI()
    app.state.job_store = JobStore(directory=str(tmp_path / "jobs"))
    pipeline = _OkPipeline()
    app.state.pipeline = pipeline

    class FakeQueue:
        _running = True

        async def enqueue(self, job_id, func, *args, **kwargs):
            return await queue(job_id, func, *args, **kwargs)

    app.state.job_queue = FakeQueue()
    app.include_router(jobs_router)
    app.include_router(parse_router)
    return TestClient(app), pipeline


def test_jobs_queue_full_returns_429(monkeypatch, tmp_path):
    async def full(job_id, func, *a, **k):
        return False

    client, _ = _make_client(monkeypatch, tmp_path, full)
    resp = client.post("/jobs", json={"text": "5 chocolate cakes for ABC", "source": "api"})
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "5"


def test_jobs_queue_available_accepts(monkeypatch, tmp_path):
    async def ok(job_id, func, *a, **k):
        return True

    client, _ = _make_client(monkeypatch, tmp_path, ok)
    resp = client.post("/jobs", json={"text": "5 chocolate cakes for ABC", "source": "api"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "QUEUED"


def test_parse_queue_full_returns_429(monkeypatch, tmp_path):
    async def full(job_id, func, *a, **k):
        return False

    client, _ = _make_client(monkeypatch, tmp_path, full)
    resp = client.post("/parse", data={"text": "send 5 pcs chocolate cake to ABC tomorrow"})
    assert resp.status_code == 429


# ------------------------------------------------------- worker timeout


def test_worker_timeout_marks_job_failed(tmp_path, monkeypatch):
    from order_parser.core.job_queue import JobQueue

    settings = get_settings()
    monkeypatch.setattr(settings, "job_timeout_seconds", 0.2)

    store = _store(tmp_path)
    queue = JobQueue(max_workers=1, max_queue_size=10, job_store=store)

    async def _run():
        await queue.start()
        job = JobRecord(source="api", input_type="text", status=JobStatus.QUEUED)
        store.save(job)

        async def _hang():
            await asyncio.sleep(5)

        await queue.enqueue(job.job_id, _hang)
        for _ in range(100):
            rec = store.get(job.job_id)
            if rec is not None and rec.status == JobStatus.FAILED:
                break
            await asyncio.sleep(0.05)
        rec = store.get(job.job_id)
        await queue.stop()
        return rec

    rec = asyncio.run(_run())
    assert rec is not None
    assert rec.status == JobStatus.FAILED
    assert rec.error_code == "RESOURCE-001"
