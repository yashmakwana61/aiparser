from __future__ import annotations

import hashlib
import time
from typing import Any

import structlog

from order_parser.core import metrics
from order_parser.core.job import JobRecord, JobStatus, generate_job_id, utc_now_iso
from order_parser.core.job_store import JobStore

logger = structlog.get_logger(__name__)


def hash_content(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()[:16]


def fingerprint_telegram(chat_id: int | str, message_id: int | str, bot_id: str = "") -> str:
    """Strong Telegram deduplication key as per spec: bot + chat + message_id"""
    return f"tg:{bot_id}:{chat_id}:{message_id}" if bot_id else f"tg:{chat_id}:{message_id}"


def fingerprint_email(message_id: str, content_hash: str = "") -> str:
    # Normalized Message-ID is primary; fallback to content hash
    mid = (message_id or "").strip().strip("<>").casefold()
    return f"email:{mid}" if mid else f"email:sha256:{content_hash}"


def check_duplicate(job_store: JobStore, input_hash: str = "", source_message_id: str = "", window_hours: int = 24):
    """Return existing job if duplicate found via hash or source_message_id."""
    if source_message_id:
        # Search by source_message_id exact match (File Store scan)
        for rec in job_store.list():
            if rec.source_message_id == source_message_id:
                # within window?
                from datetime import datetime, timedelta, timezone
                try:
                    created = datetime.fromisoformat(rec.created_at)
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
                    if created < cutoff:
                        continue
                except Exception:
                    pass
                if rec.status in (JobStatus.COMPLETED, JobStatus.NEEDS_REVIEW, JobStatus.QUEUED, JobStatus.PROCESSING):
                    return rec
    if input_hash:
        existing = job_store.find_by_hash(input_hash, window_hours=window_hours)
        if existing and existing.status in (JobStatus.COMPLETED, JobStatus.NEEDS_REVIEW, JobStatus.QUEUED, JobStatus.PROCESSING):
            return existing
    return None


def _retry_delay_seconds(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff: base * 2^attempt, capped. attempt is 0-based."""
    try:
        delay = float(base) * (2 ** max(int(attempt), 0))
    except OverflowError:
        return float(cap)
    return min(float(cap), delay)


def run_job_sync(
    job_store: JobStore,
    pipeline,
    job: JobRecord,
    parsed,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Synchronous job execution with full state machine transitions.
    This is the single source of truth for job processing — used by API, Telegram, and Email.

    Transitions: RECEIVED -> QUEUED -> PROCESSING -> (RESOLVING etc via pipeline) -> COMPLETED/NEEDS_REVIEW/FAILED
    Transient failures (TRANSIENT per error taxonomy) are retried with
    exponential backoff through the RETRYING state, up to job_max_retries.
    PERMANENT and REVIEW_REQUIRED outcomes are never retried.
    Updates job_store at each transition with job_id binding for observability.
    """
    raw = raw or {}
    # Ensure QUEUED
    try:
        if job.status == JobStatus.RECEIVED:
            job.status = JobStatus.QUEUED
            job_store.save(job)
    except Exception:
        logger.exception("job_runner.queued_save_failed", job_id=job.job_id)

    # PROCESSING
    try:
        job.status = JobStatus.PROCESSING
        job.started_at = utc_now_iso()
        job.updated_at = utc_now_iso()
        job_store.save(job)
    except Exception:
        logger.exception("job_runner.processing_save_failed", job_id=job.job_id)

    # Bind job_id for downstream logging
    structlog.contextvars.bind_contextvars(job_id=job.job_id)
    from order_parser.config import get_settings as _get_settings

    _settings = _get_settings()
    max_retries = max(int(getattr(_settings, "job_max_retries", 3) or 0), 0)
    backoff_base = float(getattr(_settings, "job_retry_backoff_seconds", 1.0) or 0)
    backoff_cap = float(getattr(_settings, "job_retry_max_backoff_seconds", 30.0) or 0)
    job_timeout = float(getattr(_settings, "job_timeout_seconds", 300.0) or 0)
    overall_start = time.monotonic()
    start = overall_start
    try:
        # Fine-grained states: we log EXTRACTING/NORMALIZING etc but pipeline already did extraction
        # For now we transition through the conceptual states quickly before pipeline call
        for stage in [JobStatus.EXTRACTING, JobStatus.NORMALIZING, JobStatus.VALIDATING, JobStatus.RESOLVING]:
            try:
                # Only allow valid transitions; if not allowed, skip
                from order_parser.core.job import can_transition
                if can_transition(job.status, stage):
                    job.status = stage
                    job.updated_at = utc_now_iso()
                    job_store.save(job)
            except Exception:
                pass

        attempt = 0
        while True:
            # Job-level timeout guard (resource control): abort before another
            # attempt if the overall budget is already exhausted.
            if job_timeout > 0 and (time.monotonic() - overall_start) >= job_timeout:
                raise TimeoutError(f"job exceeded {job_timeout}s timeout budget")
            try:
                result = pipeline.process(job.source, job.input_type, parsed, raw=raw)
                break  # success path continues below
            except Exception as exc:
                from order_parser.core.errors import classify_error, ErrorCategory

                code = getattr(exc, "error_code", None) or "SYS-001"
                category = classify_error(str(code))
                retryable = category == ErrorCategory.TRANSIENT and attempt < max_retries
                if not retryable:
                    raise
                attempt += 1
                job.retry_count = attempt
                try:
                    from order_parser.core.job import can_transition as _can

                    if _can(job.status, JobStatus.RETRYING):
                        job.status = JobStatus.RETRYING
                    else:
                        job.status = JobStatus.RETRYING
                except Exception:
                    job.status = JobStatus.RETRYING
                job.error_code = str(code)
                job.error_message = f"transient failure (attempt {attempt}/{max_retries}): {str(exc)[:300]}"
                job.updated_at = utc_now_iso()
                try:
                    job_store.save(job)
                except Exception:
                    logger.exception("job_runner.retry_save_failed", job_id=job.job_id)
                metrics.incr("jobs_retried_total", source=job.source)
                logger.warning(
                    "job_runner.retrying",
                    job_id=job.job_id,
                    attempt=attempt,
                    error_code=str(code),
                    error=str(exc)[:200],
                )
                delay = _retry_delay_seconds(attempt - 1, backoff_base, backoff_cap)
                # Honor the overall timeout budget while sleeping.
                if job_timeout > 0:
                    remaining = job_timeout - (time.monotonic() - overall_start)
                    if remaining <= 0:
                        raise TimeoutError(f"job exceeded {job_timeout}s timeout budget")
                    delay = min(delay, remaining)
                if delay > 0:
                    time.sleep(delay)
                try:
                    from order_parser.core.job import can_transition as _can2

                    if _can2(job.status, JobStatus.PROCESSING):
                        job.status = JobStatus.PROCESSING
                    else:
                        job.status = JobStatus.PROCESSING
                    job.updated_at = utc_now_iso()
                    job_store.save(job)
                except Exception:
                    pass
                continue

        # After a retry the job sits in PROCESSING; re-drive the conceptual
        # stage transitions so the success mapping below (RESOLVING ->
        # READY_FOR_ODOO -> ...) follows only allowed transitions.
        if attempt > 0:
            from order_parser.core.job import can_transition as _can3

            for stage in [JobStatus.EXTRACTING, JobStatus.NORMALIZING, JobStatus.VALIDATING, JobStatus.RESOLVING]:
                try:
                    if _can3(job.status, stage):
                        job.status = stage
                        job.updated_at = utc_now_iso()
                        job_store.save(job)
                except Exception:
                    pass

        elapsed = int((time.monotonic() - start) * 1000)
        job.processing_time_ms = elapsed
        job.result = result
        job.confidence = float(result.get("confidence") or result.get("confidence", 0) or 0)
        job.customer_detected = str(result.get("customer") or "")
        job.items_detected = int(result.get("items") or 0)
        job.missing_fields = list(result.get("missing_information") or [])
        job.odoo_order_name = result.get("sales_order")
        job.odoo_order_id = result.get("sales_order")
        status = result.get("status")
        if status == "success":
            # READY_FOR_ODOO -> SENT_TO_ODOO -> COMPLETED
            for target in [JobStatus.READY_FOR_ODOO, JobStatus.SENT_TO_ODOO, JobStatus.COMPLETED]:
                try:
                    if __import__("order_parser.core.job", fromlist=["can_transition"]).can_transition(job.status, target):
                        job.status = target
                except Exception:
                    job.status = target
            job.error_code = None
            job.error_message = None
            job.review_required = False
            job.review_reason = None
        elif status in ("pending", "review"):
            job.status = JobStatus.NEEDS_REVIEW
            job.review_required = True
            job.review_reason = result.get("message") or "; ".join(result.get("resolution_blocked") or [])
            # Map to error taxonomy codes
            rr = (job.review_reason or "").lower()
            if "product" in rr or "ambiguous" in rr:
                job.error_code = "RES-002"
            elif "customer" in rr:
                job.error_code = "RES-001"
            elif "ocr" in rr:
                job.error_code = "OCR-002"
            else:
                job.error_code = "RES-001"
        else:
            job.status = JobStatus.FAILED
            job.error_code = "SYS-001"
            job.error_message = str(result.get("message") or "")[:500]
            job.review_required = False

        job.completed_at = utc_now_iso()
        job.updated_at = utc_now_iso()
        job_store.save(job)
        metrics.incr("jobs_processed_total", status=job.status.value, source=job.source)
        logger.info("job_runner.completed", job_id=job.job_id, status=job.status.value, elapsed_ms=elapsed)
        return result
    except Exception as exc:
        logger.exception("job_runner.failed", job_id=job.job_id)
        try:
            job.status = JobStatus.FAILED
            # Preserve resource-exhaustion semantics only for our own budget
            # guard; a TimeoutError raised by a downstream provider keeps the
            # generic transient SYS-001 so it stays retryable upstream.
            _msg = str(exc)
            _is_budget = isinstance(exc, TimeoutError) and "timeout budget" in _msg
            job.error_code = "RESOURCE-001" if _is_budget else "SYS-001"
            job.error_message = _msg[:500]
            job.completed_at = utc_now_iso()
            job.updated_at = utc_now_iso()
            job_store.save(job)
        except Exception:
            pass
        metrics.incr("jobs_failed_total", source=job.source)
        return {"status": "error", "job_id": job.job_id, "message": str(exc), "error_code": "SYS-001"}
    finally:
        structlog.contextvars.unbind_contextvars("job_id")


def _allocate_job_id(job_store: JobStore) -> str:
    """Durable job id via the store; falls back to the in-memory generator."""
    next_id = getattr(job_store, "next_job_id", None)
    if callable(next_id):
        return next_id()
    return generate_job_id()


def create_job(
    job_store: JobStore,
    source: str,
    input_type: str,
    source_message_id: str = "",
    sender_id: str = "",
    file_name: str = "",
    file_size: int = 0,
    input_hash: str = "",
    parser_version: str = "1.2.0",
    ocr_provider: str = "",
) -> tuple[JobRecord, JobRecord | None]:
    """
    Create a new JobRecord, checking for duplicates.
    Returns (new_job, duplicate_of). If duplicate_of is not None, caller should NOT process and should return duplicate response.
    """
    # Idempotency check before creation
    dup = check_duplicate(job_store, input_hash=input_hash, source_message_id=source_message_id)
    if dup is not None:
        metrics.incr("jobs_duplicate_blocked_total", source=source)
        logger.info("job_runner.duplicate_blocked", source_message_id=source_message_id, input_hash=input_hash, duplicate_of=dup.job_id)
        return dup, dup

    job = JobRecord(
        job_id=_allocate_job_id(job_store),
        source=source,
        source_message_id=source_message_id,
        sender_id=sender_id,
        input_type=input_type,
        file_name=file_name,
        file_size=file_size,
        input_hash=input_hash,
        parser_version=parser_version,
        ocr_provider=ocr_provider,
        status=JobStatus.RECEIVED,
    )
    job_store.save(job)
    metrics.incr("jobs_created_total", source=source)
    logger.info("job_runner.created", job_id=job.job_id, source=source, input_type=input_type, source_message_id=source_message_id)
    return job, None
