from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_lock = threading.Lock()
_counter = 0
_last_date = ""


class JobStatus(str, Enum):
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    EXTRACTING = "EXTRACTING"
    NORMALIZING = "NORMALIZING"
    VALIDATING = "VALIDATING"
    RESOLVING = "RESOLVING"
    READY_FOR_ODOO = "READY_FOR_ODOO"
    SENT_TO_ODOO = "SENT_TO_ODOO"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


# For compatibility with pipeline's pending/review statuses
PIPELINE_ALIAS: dict[str, JobStatus] = {
    "success": JobStatus.COMPLETED,
    "pending": JobStatus.NEEDS_REVIEW,
    "review": JobStatus.NEEDS_REVIEW,
    "error": JobStatus.FAILED,
}

ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.RECEIVED: {JobStatus.QUEUED, JobStatus.FAILED},
    JobStatus.QUEUED: {JobStatus.PROCESSING, JobStatus.FAILED},
    JobStatus.PROCESSING: {JobStatus.EXTRACTING, JobStatus.FAILED, JobStatus.RETRYING},
    JobStatus.EXTRACTING: {JobStatus.NORMALIZING, JobStatus.NEEDS_REVIEW, JobStatus.FAILED, JobStatus.RETRYING},
    JobStatus.NORMALIZING: {JobStatus.VALIDATING, JobStatus.NEEDS_REVIEW, JobStatus.FAILED, JobStatus.RETRYING},
    JobStatus.VALIDATING: {JobStatus.RESOLVING, JobStatus.NEEDS_REVIEW, JobStatus.FAILED, JobStatus.RETRYING},
    JobStatus.RESOLVING: {JobStatus.READY_FOR_ODOO, JobStatus.NEEDS_REVIEW, JobStatus.FAILED, JobStatus.RETRYING},
    JobStatus.READY_FOR_ODOO: {JobStatus.SENT_TO_ODOO, JobStatus.NEEDS_REVIEW, JobStatus.FAILED, JobStatus.RETRYING},
    JobStatus.SENT_TO_ODOO: {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.RETRYING},
    JobStatus.COMPLETED: set(),
    JobStatus.NEEDS_REVIEW: {JobStatus.PROCESSING, JobStatus.FAILED, JobStatus.COMPLETED},
    JobStatus.FAILED: {JobStatus.RETRYING, JobStatus.QUEUED},
    JobStatus.RETRYING: {JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.FAILED},
}

TERMINAL_STATUSES: set[JobStatus] = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.NEEDS_REVIEW,
}


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def generate_job_id() -> str:
    """Generate ORD-YYYYMMDD-###### monotonic per day."""
    global _counter, _last_date
    now = datetime.now(timezone.utc)
    date_part = now.strftime("%Y%m%d")
    with _lock:
        if date_part != _last_date:
            _last_date = date_part
            _counter = 0
        _counter += 1
        seq = _counter
        # also incorporate pid to avoid collision across processes without shared counter file
        pid_suffix = os.getpid() % 100
        return f"ORD-{date_part}-{seq:06d}"


class JobRecord(BaseModel):
    job_id: str = Field(default_factory=generate_job_id)
    source: str = ""
    source_message_id: str = ""
    sender_id: str = ""
    received_at: str = Field(default_factory=utc_now_iso)
    started_at: str | None = None
    completed_at: str | None = None

    input_type: str = ""
    file_name: str = ""
    file_size: int = 0

    parser_version: str = "1.2.0"
    model_version: str = ""
    ocr_provider: str = ""

    processing_time_ms: int | None = None
    retry_count: int = 0

    customer_detected: str = ""
    items_detected: int = 0
    missing_fields: list[str] = Field(default_factory=list)

    odoo_order_id: int | str | None = None
    odoo_order_name: str | None = None

    status: JobStatus = JobStatus.RECEIVED
    error_code: str | None = None
    error_message: str | None = None

    # Extended diagnostics
    input_hash: str = ""
    result: dict[str, Any] | None = None
    confidence: float | None = None
    review_required: bool = False
    review_reason: str | None = None

    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    def transition(self, target: JobStatus) -> None:
        if not can_transition(self.status, target):
            raise ValueError(f"Invalid job transition {self.status} -> {target}")
        self.status = target
        self.updated_at = utc_now_iso()
        if target == JobStatus.PROCESSING and not self.started_at:
            self.started_at = utc_now_iso()
        if target in TERMINAL_STATUSES:
            self.completed_at = utc_now_iso()
