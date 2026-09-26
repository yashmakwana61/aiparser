from __future__ import annotations

import structlog
import uuid

from order_parser.sessions.models import (
    ACTIVE_STATUSES,
    EXPIRABLE_STATUSES,
    SessionAttachment,
    SessionMessage,
    SessionStatus,
    StaffIdentity,
    StaffSession,
    can_transition,
    utc_now_iso,
)
from order_parser.core import metrics
from order_parser.sessions.store import SessionStore

logger = structlog.get_logger(__name__)


class SessionError(Exception):
    """Base error for session lifecycle violations."""


class SessionAccessError(SessionError):
    """The acting staff member does not own the session."""


class SessionActiveError(SessionError):
    """The staff member already has an active order session."""

    def __init__(self, session: StaffSession) -> None:
        super().__init__(f"session {session.session_id} is already active")
        self.session = session


class SessionManager:
    """Lifecycle and transition guards for staff order sessions."""

    def __init__(self, store: SessionStore, timeout_minutes: int = 60) -> None:
        self.store = store
        self.timeout_minutes = max(1, int(timeout_minutes))

    # ------------------------------------------------------------------ create

    def start_session(self, identity: StaffIdentity, chat_id: int | None = None) -> StaffSession:
        existing = self.get_collecting_session(identity.staff_id)
        if existing is not None:
            raise SessionActiveError(existing)
        session = StaffSession(
            session_id=utc_now_iso().replace("-", "").replace(":", "")[-10:] + uuid.uuid4().hex[:8],
            staff_id=identity.staff_id,
            staff_name=identity.display_name,
            telegram_user_id=identity.telegram_user_id,
            chat_id=chat_id,
            status=SessionStatus.NEW,
        )
        self._transition(session, SessionStatus.COLLECTING)
        self.store.save(session)
        logger.info(
            "session.started",
            session_id=session.session_id,
            staff_id=session.staff_id,
            channel=session.channel,
        )
        return session

    # ------------------------------------------------------------------ lookup

    def get(self, session_id: str) -> StaffSession:
        session = self.store.get(session_id)
        if session is None:
            raise SessionError(f"session {session_id} not found")
        return session

    def get_collecting_session(self, staff_id: str | None = None) -> StaffSession | None:
        for session in self._ordered(self.store.list_active()):
            if session.status != SessionStatus.COLLECTING:
                continue
            if staff_id is not None and session.staff_id != staff_id:
                continue
            return session
        return None

    def get_latest_for_staff(self, staff_id: str) -> StaffSession | None:
        for session in self._ordered(self.store.list_by_staff(staff_id)):
            if session.status in ACTIVE_STATUSES:
                return session
        return None

    @staticmethod
    def _ordered(sessions: list[StaffSession]) -> list[StaffSession]:
        return sorted(sessions, key=lambda s: s.updated_at, reverse=True)

    # ----------------------------------------------------------------- capture

    def add_text(
        self,
        session_id: str,
        staff_id: str,
        text: str,
        telegram_message_id: int | None = None,
    ) -> StaffSession:
        session = self._owned(session_id, staff_id)
        self._require_status(session, SessionStatus.COLLECTING)
        session.messages.append(
            SessionMessage(text=text, sender_staff_id=staff_id, telegram_message_id=telegram_message_id)
        )
        self._touch_and_save(session)
        return session

    def add_attachment(self, session_id: str, staff_id: str, attachment: SessionAttachment) -> StaffSession:
        session = self._owned(session_id, staff_id)
        self._require_status(session, SessionStatus.COLLECTING)
        if any(existing.sha256 == attachment.sha256 for existing in session.attachments):
            logger.info(
                "session.duplicate_attachment_ignored",
                session_id=session_id,
                sha256=attachment.sha256,
            )
            return session
        session.attachments.append(attachment)
        self._touch_and_save(session)
        return session

    # --------------------------------------------------------------- lifecycle

    def finish(self, session_id: str, staff_id: str) -> StaffSession:
        session = self._owned(session_id, staff_id)
        if not session.messages and not session.attachments:
            raise SessionError("cannot finish an empty session")
        self._transition(session, SessionStatus.PROCESSING)
        self._touch_and_save(session)
        return session

    def cancel(self, session_id: str, staff_id: str) -> StaffSession:
        session = self._owned(session_id, staff_id)
        if session.status == SessionStatus.CANCELLED:
            return session
        self._transition(session, SessionStatus.CANCELLED)
        self._touch_and_save(session)
        return session

    def advance(self, session_id: str, to_status: SessionStatus, updates: dict | None = None) -> StaffSession:
        """System-driven transition (pipeline outcome, expiry sweeper)."""
        session = self.get(session_id)
        self._transition(session, to_status)
        for key, value in (updates or {}).items():
            setattr(session, key, value)
        self._touch_and_save(session)
        return session

    def expire_stale(self, now_iso: str | None = None) -> list[StaffSession]:
        """Expire sessions idle beyond the timeout. Expired sessions never
        reach CREATING_ORDER; the sweeper is the only caller."""
        from datetime import datetime, timedelta, timezone

        now = (
            datetime.fromisoformat(now_iso)
            if now_iso
            else datetime.now(timezone.utc)
        )
        cutoff = now - timedelta(minutes=self.timeout_minutes)
        expired: list[StaffSession] = []
        for session in self.store.list_active():
            if session.status not in EXPIRABLE_STATUSES:
                continue
            try:
                updated = datetime.fromisoformat(session.updated_at)
            except ValueError:
                continue
            if updated >= cutoff:
                continue
            self._transition(session, SessionStatus.EXPIRED)
            self._touch_and_save(session)
            expired.append(session)
            logger.info(
                "session.expired",
                session_id=session.session_id,
                staff_id=session.staff_id,
            )
        return expired

    # ---------------------------------------------------------------- internals

    def _owned(self, session_id: str, staff_id: str) -> StaffSession:
        session = self.get(session_id)
        if staff_id and session.staff_id != staff_id:
            raise SessionAccessError(f"session {session_id} belongs to another staff member")
        return session

    @staticmethod
    def _require_status(session: StaffSession, expected: SessionStatus) -> None:
        if session.status != expected:
            raise SessionError(f"session {session.session_id} is {session.status.value}, expected {expected.value}")

    @staticmethod
    def _transition(session: StaffSession, target: SessionStatus) -> None:
        if not can_transition(session.status, target):
            raise SessionError(f"illegal transition {session.status.value} -> {target.value}")
        metrics.incr(
            "session_status_transitions_total",
            status_from=session.status.value,
            status_to=target.value,
        )
        session.status = target

    def _touch_and_save(self, session: StaffSession) -> None:
        session.updated_at = utc_now_iso()
        self.store.save(session)
