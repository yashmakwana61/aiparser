from __future__ import annotations

import email
import hashlib
import imaplib
from email.header import decode_header
from email.message import Message
from typing import Any

import structlog
from bs4 import BeautifulSoup

from order_parser.config import get_settings
from order_parser.core.email_state_store import EmailStateStore
from order_parser.core.job_runner import create_job as runner_create_job
from order_parser.core.job_runner import fingerprint_email, hash_content
from order_parser.core.job_runner import run_job_sync
from order_parser.core.job_store import JobStore
from order_parser.processors.detector import IMAGE_EXTENSIONS, InputType, detect_input_type
from order_parser.processors.excel_processor import ExcelProcessor
from order_parser.processors.image_processor import ImageProcessor
from order_parser.processors.pdf_processor import PDFProcessor
from order_parser.processors.text_processor import TextProcessor
from order_parser.services.pipeline import OrderPipeline

logger = structlog.get_logger(__name__)

SUPPORTED_ATTACHMENT_SUFFIXES = IMAGE_EXTENSIONS | {".pdf", ".xls", ".xlsx", ".xlsm"}


def normalize_message_id(value: str | None) -> str:
    return (value or "").strip().strip("<>").casefold()


def content_hash(raw_email: bytes) -> str:
    return hashlib.sha256(raw_email).hexdigest()


def backoff_delay(failure_count: int, base_seconds: int, cap_seconds: int) -> int:
    """Exponential poll backoff: base * 2^n, capped."""
    return min(int(cap_seconds), int(base_seconds) * (2 ** min(max(failure_count, 0), 16)))


class EmailHandler:
    """IMAP poller plus raw-email processing for plain-text/HTML bodies and
    PDF, Excel and image attachments.

    Phase 9 hardening: per-message dedup via a persistent seen-store (Message-
    ID or content hash), attachment size/type guards applied before routing,
    and connection timeouts on the IMAP socket.
    """

    def __init__(
        self,
        pipeline: OrderPipeline,
        state_store: EmailStateStore | None = None,
        processors=None,
        job_store: JobStore | None = None,
    ):
        self.pipeline = pipeline
        self.job_store = job_store
        if processors is not None:
            self.text_processor = processors.text
            self.image_processor = processors.image
            self.pdf_processor = processors.pdf
            self.excel_processor = processors.excel
        else:
            self.text_processor = TextProcessor()
            self.image_processor = ImageProcessor()
            self.pdf_processor = PDFProcessor()
            self.excel_processor = ExcelProcessor()
        settings = get_settings()
        self.state_store = state_store or EmailStateStore(
            getattr(settings, "email_state_db_path", "") or None
        )
        self.seen_window_days = int(getattr(settings, "email_seen_window_days", 7))
        max_mb = int(getattr(settings, "email_max_attachment_mb", 10))
        self.max_attachment_bytes = max(1, max_mb) * 1024 * 1024

    def _run_via_job(
        self,
        source_message_id: str,
        input_type: str,
        parsed,
        raw: dict[str, Any],
        file_name: str = "",
        file_size: int = 0,
        input_hash: str = "",
        sender_id: str = "",
    ) -> dict[str, Any]:
        if self.job_store is not None:
            job, duplicate = runner_create_job(
                self.job_store,
                source="email",
                input_type=input_type,
                source_message_id=source_message_id,
                sender_id=sender_id,
                file_name=file_name,
                file_size=file_size,
                input_hash=input_hash or hash_content(str(raw.get("body") or file_name)),
            )
            if duplicate is not None:
                logger.info("email.duplicate_job_blocked", job_id=duplicate.job_id, source_message_id=source_message_id)
                if duplicate.result is not None:
                    return duplicate.result
                return {"status": "duplicate", "job_id": duplicate.job_id, "duplicate_of": duplicate.job_id, "message": "Duplicate email already ingested"}
            return run_job_sync(self.job_store, self.pipeline, job, parsed, raw=raw)
        return self.pipeline.process("email", input_type, parsed, raw)

    def poll(self) -> int:
        """Fetch unread messages from the inbox and process them."""
        settings = get_settings()
        if not settings.email_imap_host or not settings.email_username:
            logger.info("email.polling_disabled")
            return 0
        mail = imaplib.IMAP4_SSL(
            settings.email_imap_host,
            settings.email_imap_port,
            timeout=float(getattr(settings, "email_imap_timeout_seconds", 30.0)),
        )
        try:
            mail.login(settings.email_username, settings.email_password)
            mail.select("INBOX")
            status, data = mail.uid("search", None, "UNSEEN")
            if status != "OK":
                return 0
            uids = data[0].split()
            processed = 0
            for uid in uids:
                ok, msg_data = mail.uid("fetch", uid, "(RFC822)")
                if ok != "OK" or not msg_data or msg_data[0] is None:
                    continue
                try:
                    results = self.process_raw_email(bytes(msg_data[0][1]))
                    mail.uid("store", uid, "+FLAGS", "\\Seen")
                    processed += len(results)
                except Exception:
                    logger.exception("email.message_failed", uid=uid)
            return processed
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def process_raw_email(self, raw_email: bytes) -> list[dict[str, Any]]:
        # Message-level dedup (Phase 9): Message-ID when present, content
        # hash otherwise. Re-delivered or re-flagged emails are skipped.
        message = email.message_from_bytes(raw_email)
        subject = _decode_header(message.get("Subject", ""))
        sender = str(message.get("From", ""))
        message_key = normalize_message_id(message.get("Message-ID")) or f"sha256:{content_hash(raw_email)}"
        if self.state_store.already_seen(message_key, window_days=self.seen_window_days):
            logger.info("email.duplicate_skipped", message_key=message_key[:32])
            return [
                {
                    "status": "duplicate",
                    "message": "email already ingested",
                    "source": "email",
                    "message_key": message_key[:64],
                }
            ]

        body = self._extract_body(message)
        attachments = list(self._extract_attachments(message))
        raw: dict[str, Any] = {"subject": subject, "sender": sender, "body": body[:20000]}

        results: list[dict[str, Any]] = []
        for filename, content in attachments:
            suffix = "." in filename and ("." + filename.rsplit(".", 1)[1].lower()) or ""
            if len(content) > self.max_attachment_bytes:
                logger.warning("email.attachment_too_large", filename=filename, size=len(content))
                results.append(
                    {
                        "status": "error",
                        "reason": "attachment_too_large",
                        "message": f"attachment '{filename}' exceeds the size limit",
                        "source": "email",
                        "filename": filename,
                    }
                )
                continue
            if suffix not in SUPPORTED_ATTACHMENT_SUFFIXES:
                logger.warning("email.attachment_unsupported_type", filename=filename)
                results.append(
                    {
                        "status": "error",
                        "reason": "unsupported_attachment_type",
                        "message": f"attachment '{filename}' has an unsupported type",
                        "source": "email",
                        "filename": filename,
                    }
                )
                continue
            input_type = detect_input_type(None, filename)
            try:
                parsed = self._route(input_type, content, filename)
                # Use Job API as source of truth — idempotency via Message-ID + filename
                source_mid = fingerprint_email(message.get("Message-ID") or message_key, hash_content(content))
                result = self._run_via_job(
                    f"{source_mid}:{filename}",
                    input_type.value,
                    parsed,
                    {**raw, "filename": filename},
                    file_name=filename,
                    file_size=len(content),
                    input_hash=hash_content(content),
                    sender_id=sender,
                )
                results.append(result)
            except Exception as exc:
                logger.exception("email.attachment_failed", filename=filename)
                results.append({"status": "error", "message": str(exc), "source": "email", "input_type": input_type.value, "filename": filename})
        if not attachments and body.strip():
            try:
                parsed = self.text_processor.process(body)
                source_mid = fingerprint_email(message.get("Message-ID") or message_key, hash_content(body))
                result = self._run_via_job(
                    source_mid,
                    "text",
                    parsed,
                    raw,
                    file_name="",
                    file_size=len(body.encode("utf-8")),
                    input_hash=hash_content(body),
                    sender_id=sender,
                )
                results.append(result)
            except Exception as exc:
                logger.exception("email.body_failed")
                results.append({"status": "error", "message": str(exc), "source": "email", "input_type": "text"})

        # Consume the whole message once handled: individual attachment
        # failures were already reported as error results above.
        self.state_store.mark_seen(message_key, source="email")
        return results

    def _route(self, input_type: InputType, data: bytes, filename: str):
        if input_type == InputType.PDF:
            return self.pdf_processor.process(data, filename)
        if input_type == InputType.EXCEL:
            return self.excel_processor.process(data, filename)
        if input_type == InputType.IMAGE:
            return self.image_processor.process(data, filename)
        return self.text_processor.process(data.decode("utf-8", errors="replace"))

    def _extract_body(self, message: Message) -> str:
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            for part in message.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                        return BeautifulSoup(html, "html.parser").get_text("\n")
        payload = message.get_payload(decode=True)
        if payload:
            return payload.decode(message.get_content_charset() or "utf-8", errors="replace")
        return ""

    @staticmethod
    def _extract_attachments(message: Message):
        for part in message.walk():
            filename = part.get_filename()
            if not filename:
                continue
            payload = part.get_payload(decode=True)
            if payload:
                yield filename, bytes(payload)


def _decode_header(value: str) -> str:
    parts = decode_header(value or "")
    return "".join(
        part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else str(part)
        for part, charset in parts
    )