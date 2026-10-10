from __future__ import annotations

import hashlib
import time
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from pydantic import BaseModel

from order_parser.api.auth import security_dependencies
from order_parser.core import metrics
from order_parser.core.job import JobRecord, JobStatus, generate_job_id, utc_now_iso
from order_parser.core.job_runner import create_job as runner_create_job
from order_parser.core.job_runner import hash_content as runner_hash
from order_parser.core.job_runner import run_job_sync
from order_parser.core.job_store import JobStore
from order_parser.processors.detector import detect_input_type
from order_parser.processors.excel_processor import ExcelProcessor
from order_parser.processors.image_processor import ImageProcessor
from order_parser.processors.pdf_processor import PDFProcessor
from order_parser.processors.text_processor import TextProcessor

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=security_dependencies())
parse_router = APIRouter(prefix="/parse", tags=["parse"], dependencies=security_dependencies())


class ParseRequest(BaseModel):
    text: str | None = None
    source: str = "api"
    input_type: str | None = None


def _get_stores(request: Request) -> tuple[JobStore, Any]:
    job_store = getattr(request.app.state, "job_store", None)
    pipeline = getattr(request.app.state, "pipeline", None)
    if job_store is None:
        # lazy fallback: create per-request (tests without lifespan)
        from order_parser.core.job_store import JobStore as JS

        job_store = JS()
        request.app.state.job_store = job_store
    if pipeline is None:
        try:
            from order_parser.services.pipeline import OrderPipeline

            pipeline = OrderPipeline()
            request.app.state.pipeline = pipeline
        except Exception:
            pipeline = None
    return job_store, pipeline


def _hash_content(data: bytes | str) -> str:
    return runner_hash(data)


def _sync_process(job_store: JobStore, pipeline, job: JobRecord, parsed) -> dict[str, Any]:
    """Delegates to the single source of truth job runner."""
    raw = {"text": getattr(parsed, "extracted_text", ""), "job_id": job.job_id}
    return run_job_sync(job_store, pipeline, job, parsed, raw=raw)


@router.post("")
async def create_job(request: Request, payload: ParseRequest) -> dict[str, Any]:
    job_store, pipeline = _get_stores(request)
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    source = payload.source or "api"
    input_type = payload.input_type or "text"
    input_hash = _hash_content(text)

    job, duplicate = runner_create_job(
        job_store,
        source=source,
        input_type=input_type,
        file_size=len(text.encode("utf-8")),
        input_hash=input_hash,
    )
    if duplicate is not None:
        return {
            "job_id": duplicate.job_id,
            "status": duplicate.status.value,
            "duplicate_of": duplicate.job_id,
            "message": "Duplicate detected; returning existing job",
        }

    # Process immediately (sync for API simplicity; queue path via background task is optional)
    processor = TextProcessor()
    parsed = processor.process(text)
    # Try async queue if available, else sync
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline not ready")
    queue = getattr(request.app.state, "job_queue", None)
    if queue is not None and getattr(queue, "_running", False):
        # enqueue background processing
        import asyncio

        async def _bg():
            _sync_process(job_store, pipeline, job, parsed)

        queued = await queue.enqueue(job.job_id, _bg)
        if not queued:
            # Backpressure: queue is full. The job stays RECEIVED and the
            # caller must retry (429 + Retry-After) — never silently dropped.
            raise HTTPException(
                status_code=429,
                detail="Job queue is full; try again later",
                headers={"Retry-After": "5"},
            )
        return {"job_id": job.job_id, "status": JobStatus.QUEUED.value, "message": "Queued for processing"}
    else:
        result = _sync_process(job_store, pipeline, job, parsed)
        return {
            "job_id": job.job_id,
            "status": job.status.value,
            "result": result,
            "review_required": job.review_required,
            "error": job.error_message,
        }


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    job_store, _ = _get_stores(request)
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "source": job.source,
        "input_type": job.input_type,
        "received_at": job.received_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "processing_time_ms": job.processing_time_ms,
        "retry_count": job.retry_count,
        "customer_detected": job.customer_detected,
        "items_detected": job.items_detected,
        "missing_fields": job.missing_fields,
        "odoo_order_id": job.odoo_order_id,
        "odoo_order_name": job.odoo_order_name,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "confidence": job.confidence,
        "review_required": job.review_required,
        "result": job.result,
    }


@router.get("/{job_id}/actions")
async def get_job_actions(job_id: str, request: Request) -> dict[str, Any]:
    """User-facing Order Case: state + actionable issues (channel-agnostic JSON)."""
    from dataclasses import asdict, is_dataclass

    from order_parser.user_actions.case import build_case_status

    job_store, pipeline = _get_stores(request)
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = dict(job.result or {})
    record = None
    order_id = result.get("order_id")
    pending_store = getattr(pipeline, "pending_store", None)
    if order_id and pending_store is not None:
        record = pending_store.get(str(order_id))
    # Review opens heal stale pending state and re-check Odoo for
    # products created after ingest so the stored candidate lists are
    # never stale. Best-effort: failures keep the stored state.
    try:
        from order_parser.user_actions.corrections import CorrectionService

        CorrectionService(job_store, pending_store, pipeline).prepare_review(job_id)
        refreshed = job_store.get(job_id)
        if refreshed is not None:
            job = refreshed
            result = dict(job.result or {})
        if order_id and pending_store is not None:
            record = pending_store.get(str(order_id))
    except Exception:
        pass
    # Same option lists the Telegram renderer uses (best-effort).
    uom_options: list[str] | None = None
    tax_options: list[dict[str, Any]] | None = None
    odoo = getattr(pipeline, "odoo", None)
    if odoo is not None:
        try:
            uom_options = [str(u.get("name") or "") for u in (odoo.list_uoms(limit=20) or [])]
            uom_options = [u for u in uom_options if u]
        except Exception:
            uom_options = None
        try:
            tax_options = [
                {"id": t.get("id"),
                 "label": f"{t.get('name')} ({float(t.get('amount') or 0):g}%)"
                          if t.get("amount") is not None else str(t.get("name") or ""),
                 "name": str(t.get("name") or "")}
                for t in (odoo.list_sale_taxes(limit=20) or [])
            ]
            tax_options = [t for t in tax_options if t.get("id") is not None]
        except Exception:
            tax_options = None
    status = build_case_status(job, record, result,
                               uom_options=uom_options, tax_options=tax_options)

    def _jsonable(value):
        if is_dataclass(value):
            return {k: _jsonable(v) for k, v in asdict(value).items()}
        if isinstance(value, list):
            return [_jsonable(v) for v in value]
        if isinstance(value, dict):
            return {k: _jsonable(v) for k, v in value.items()}
        if hasattr(value, "value"):
            return value.value
        return value

    return {"case_id": job_id, "case": _jsonable(status)}


@router.get("")
async def list_jobs(
    request: Request,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    job_store, _ = _get_stores(request)
    jobs = job_store.list(status=status, limit=limit, offset=offset)
    total = job_store.count(status=status)
    return {
        "jobs": [j.model_dump() for j in jobs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@parse_router.post("")
async def parse_endpoint(
    request: Request,
    text: str | None = Form(default=None),
    source: str = Form(default="api"),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """Unified parse endpoint: accepts text or file upload, returns canonical job envelope."""
    job_store, pipeline = _get_stores(request)
    content: str | bytes | None = None
    input_type = "text"
    filename = ""
    file_size = 0

    if file is not None:
        data = await file.read()
        filename = file.filename or "upload.bin"
        file_size = len(data)
        # resource control
        from order_parser.config import get_settings

        settings = get_settings()
        max_bytes = int(settings.max_upload_mb) * 1024 * 1024
        if file_size > max_bytes:
            raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb} MB limit")
        # detect input type
        itype = detect_input_type(file.content_type, filename)
        input_type = itype.value
        # route to appropriate processor
        if input_type == "pdf":
            parsed = PDFProcessor().process(data, filename)
        elif input_type == "excel":
            # row limit guard
            parsed = ExcelProcessor().process(data, filename)
        elif input_type == "image":
            parsed = ImageProcessor().process(data, filename)
        else:
            content = data.decode("utf-8", errors="replace")
            parsed = TextProcessor().process(content)
        content_hash = _hash_content(data)
    else:
        raw_text = text or ""
        # also try JSON body
        if not raw_text:
            try:
                body = await request.json()
                raw_text = body.get("text") or body.get("content") or ""
                source = body.get("source") or source
            except Exception:
                pass
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise HTTPException(status_code=400, detail="Provide text or file")
        content = raw_text
        parsed = TextProcessor().process(raw_text)
        content_hash = _hash_content(raw_text)

    job, duplicate = runner_create_job(
        job_store,
        source=source,
        input_type=input_type,
        file_name=filename,
        file_size=file_size or len((content or "").encode("utf-8") if isinstance(content, str) else file_size),
        input_hash=content_hash,
    )
    if duplicate is not None:
        return {
            "job_id": duplicate.job_id,
            "status": duplicate.status.value,
            "duplicate_of": duplicate.job_id,
            "message": "Duplicate detected",
        }
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline not ready")

    queue = getattr(request.app.state, "job_queue", None)
    if queue is not None and getattr(queue, "_running", False):
        import asyncio

        async def _bg():
            _sync_process(job_store, pipeline, job, parsed)

        queued = await queue.enqueue(job.job_id, _bg)
        if not queued:
            raise HTTPException(
                status_code=429,
                detail="Job queue is full; try again later",
                headers={"Retry-After": "5"},
            )
        return {"job_id": job.job_id, "status": JobStatus.QUEUED.value, "review_required": False, "error": None, "result": None}

    result = _sync_process(job_store, pipeline, job, parsed)
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "result": result,
        "review_required": job.review_required,
        "error": job.error_message,
    }
