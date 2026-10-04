from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStatus(str, Enum):
    NEW = "NEW"
    COLLECTING = "COLLECTING"
    PROCESSING = "PROCESSING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    APPROVED = "APPROVED"
    CREATING_ORDER = "CREATING_ORDER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


ACTIVE_STATUSES: set[SessionStatus] = {
    SessionStatus.NEW,
    SessionStatus.COLLECTING,
    SessionStatus.PROCESSING,
    SessionStatus.WAITING_CONFIRMATION,
    SessionStatus.APPROVED,
    SessionStatus.CREATING_ORDER,
}

# Statuses from which the expiry sweeper may expire a session.
EXPIRABLE_STATUSES: set[SessionStatus] = {
    SessionStatus.NEW,
    SessionStatus.COLLECTING,
    SessionStatus.PROCESSING,
    SessionStatus.WAITING_CONFIRMATION,
}

ALLOWED_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.NEW: {SessionStatus.COLLECTING, SessionStatus.CANCELLED, SessionStatus.EXPIRED},
    SessionStatus.COLLECTING: {SessionStatus.PROCESSING, SessionStatus.CANCELLED, SessionStatus.EXPIRED},
    SessionStatus.PROCESSING: {
        SessionStatus.WAITING_CONFIRMATION,
        SessionStatus.CREATING_ORDER,
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.EXPIRED,
    },
    SessionStatus.WAITING_CONFIRMATION: {
        SessionStatus.APPROVED,
        SessionStatus.CANCELLED,
        SessionStatus.FAILED,
        SessionStatus.EXPIRED,
    },
    SessionStatus.APPROVED: {SessionStatus.CREATING_ORDER, SessionStatus.FAILED},
    SessionStatus.CREATING_ORDER: {SessionStatus.COMPLETED, SessionStatus.FAILED},
    SessionStatus.COMPLETED: set(),
    SessionStatus.FAILED: set(),
    SessionStatus.CANCELLED: set(),
    SessionStatus.EXPIRED: set(),
}

TERMINAL_STATUSES: set[SessionStatus] = {
    SessionStatus.COMPLETED,
    SessionStatus.FAILED,
    SessionStatus.CANCELLED,
    SessionStatus.EXPIRED,
}


class StaffIdentity(BaseModel):
    """Internal staff identity mapped from an authorized Telegram account."""

    telegram_user_id: int
    staff_id: str
    display_name: str


class SessionMessage(BaseModel):
    text: str
    sender_staff_id: str = ""
    received_at: str = Field(default_factory=utc_now_iso)
    telegram_message_id: int | None = None


class SessionAttachment(BaseModel):
    kind: str  # "photo" | "document"
    input_type: str  # image | pdf | excel | text (detector value)
    filename: str
    path: str  # stored copy of the original bytes (never discarded)
    sha256: str
    size_bytes: int
    mime_type: str = ""
    caption: str = ""
    received_at: str = Field(default_factory=utc_now_iso)
    telegram_message_id: int | None = None


class StaffSession(BaseModel):
    session_id: str
    staff_id: str
    staff_name: str = ""
    telegram_user_id: int | None = None
    chat_id: int | None = None
    channel: str = "telegram"
    customer_reference: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    status: SessionStatus = SessionStatus.NEW
    messages: list[SessionMessage] = Field(default_factory=list)
    attachments: list[SessionAttachment] = Field(default_factory=list)
    extracted_fragments: list[dict[str, Any]] = Field(default_factory=list)
    combined_order: dict[str, Any] | None = None
    resolution_result: dict[str, Any] | None = None
    validation_result: dict[str, Any] | None = None
    confirmation_state: dict[str, Any] | None = None
    odoo_sale_order_id: int | str | None = None
    odoo_sale_order_name: str | None = None
    odoo_invoice_id: int | str | None = None
    error_state: dict[str, Any] | None = None
    # Linked Order Case (job) backing this session's processed order, if any.
    job_id: str | None = None


def can_transition(current: SessionStatus, target: SessionStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())
