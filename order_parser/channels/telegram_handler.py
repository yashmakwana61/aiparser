from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog
import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from order_parser.core import metrics
from order_parser.core.job import JobStatus
from order_parser.core.job_runner import create_job as runner_create_job
from order_parser.core.job_runner import fingerprint_telegram, hash_content, run_job_sync
from order_parser.core.job_store import JobStore
from order_parser.processors.detector import InputType, detect_input_type
from order_parser.processors.excel_processor import ExcelProcessor
from order_parser.processors.image_processor import ImageProcessor
from order_parser.processors.pdf_processor import PDFProcessor
from order_parser.processors.text_processor import TextProcessor
from order_parser.services.pipeline import OrderPipeline
from order_parser.sessions.manager import (
    SessionAccessError,
    SessionActiveError,
    SessionError,
    SessionManager,
)
from order_parser.sessions.models import (
    SessionAttachment,
    SessionStatus,
    StaffIdentity,
)
from order_parser.sessions.staff_registry import StaffRegistry

logger = structlog.get_logger(__name__)

COMMANDS = ("/neworder", "/done", "/cancel", "/status")

TRANSIENT_TELEGRAM_ERRORS = (telegram.error.TimedOut, telegram.error.NetworkError)


def is_transient_telegram_error(exc: Exception) -> bool:
    """True for network-level failures worth retrying.

    Note: PTB's ``BadRequest`` inherits from ``NetworkError``, so it must be
    excluded explicitly — a malformed request will never succeed on retry.
    """
    if isinstance(exc, telegram.error.BadRequest):
        return False
    return isinstance(exc, TRANSIENT_TELEGRAM_ERRORS)


async def retry_telegram_call(
    coro_factory: Callable[[], Awaitable[Any]],
    attempts: int = 3,
    delay: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Any:
    """Retry a Telegram API call across transient failures.

    Honors flood-control ``RetryAfter`` by sleeping exactly as long as
    Telegram demands. Non-transient errors propagate immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await coro_factory()
        except telegram.error.RetryAfter as exc:
            last_exc = exc
            metrics.incr("telegram_api_retries_total", outcome="flood_wait")
            wait = float(exc.retry_after) + 0.05
            logger.warning("telegram.flood_wait_retry", attempt=attempt + 1, wait_seconds=wait)
            await sleep(wait)
        except TRANSIENT_TELEGRAM_ERRORS as exc:
            if not is_transient_telegram_error(exc):
                raise
            last_exc = exc
            metrics.incr("telegram_api_retries_total", outcome="transient")
            logger.warning("telegram.retry", attempt=attempt + 1, error=str(exc))
            await sleep(delay * (attempt + 1))
    raise last_exc


async def send_message_with_retry(bot: Any, chat_id: int, text: str, attempts: int = 3) -> Any:
    """Deliver a proactive bot notification with transient-failure retries."""
    return await retry_telegram_call(
        lambda: bot.send_message(chat_id=chat_id, text=text), attempts=attempts
    )


def format_result(result: dict[str, Any]) -> str:
    status = result.get("status")
    confidence = float(result.get("confidence") or 0)
    confidence_pct = confidence * 100 if confidence <= 1.0 else confidence
    order_id = result.get("order_id")
    items_detail = result.get("items_detail") or []

    def items_block() -> str:
        if not items_detail:
            return ""
        lines = []
        for index, item in enumerate(items_detail, start=1):
            name = item.get("product_name") or "?"
            qty = item.get("quantity")
            uom = item.get("uom") or "Units"
            price = item.get("unit_price")
            price_suffix = f" @ {price:g}" if price is not None else ""
            lines.append(f"{index}. {name} x{qty} ({uom}){price_suffix}")
        return "\n" + "\n".join(lines)

    missing = result.get("missing_information") or []
    missing_block = ""
    if missing:
        missing_block = "\nMissing (Odoo defaults will apply): " + ", ".join(missing)

    if status == "success":
        return (
            f"Order {result.get('sales_order') or '?'} created for {result.get('customer') or '?'} "
            f"with {result.get('items', 0)} item(s)."
            f"{items_block()}"
            f"{missing_block}"
        )
    if status == "pending":
        return (
            f"Order {order_id} awaits confirmation (confidence {confidence_pct:.0f}%).\n"
            f"Customer: {result.get('customer') or 'not provided'}\n"
            f"Items:{items_block()}"
            f"{missing_block}\n"
            f"\nReply with: CONFIRM {order_id}"
        )
    if status == "review":
        return (
            f"Order {order_id} was sent for manual review (confidence {confidence_pct:.0f}%).\n"
            f"Customer: {result.get('customer') or 'not provided'}\n"
            f"Items:{items_block()}"
            f"{missing_block}"
        )
    if status == "rejected":
        return f"Order {order_id} has been rejected."
    return f"Processing error: {result.get('message', 'unknown error')}"


def collection_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✓ Finish Order", callback_data="sess:finish"),
                InlineKeyboardButton("✕ Cancel Order", callback_data="sess:cancel"),
            ]
        ]
    )


def confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✓ Confirm Order", callback_data="sess:confirm")],
            [
                InlineKeyboardButton("✎ Correct", callback_data="sess:correct"),
                InlineKeyboardButton("✕ Cancel", callback_data="sess:cancel"),
            ],
        ]
    )


def review_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Keyboard for review-blocked orders: explain + escape hatch only.

    No confirm button here on purpose — a blocked order usually cannot be
    created in Odoo (e.g. ambiguous product has no product_id), and the
    system never guesses.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("❓ Why blocked?", callback_data=f"sess:why:{session_id}")],
            [InlineKeyboardButton("✕ Cancel", callback_data="sess:cancel")],
        ]
    )


class TelegramHandler:
    """Processes Telegram updates: text, photos, PDF and Excel documents,
    plus CONFIRM/REJECT replies for pending orders.

    With a :class:`SessionManager` wired (Phase 3), authorized staff can
    collect multiple messages/attachments into one order session
    (``/neworder`` … ``/done``); inputs are captured instead of being
    processed as independent orders.
    """

    def __init__(
        self,
        pipeline: OrderPipeline,
        session_manager: SessionManager | None = None,
        staff_registry: StaffRegistry | None = None,
        session_service: Any | None = None,
        job_store: JobStore | None = None,
        job_queue: Any | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.session_manager = session_manager
        self.staff_registry = staff_registry
        self.session_service = session_service
        self.job_store = job_store
        self.job_queue = job_queue
        self.text_processor = TextProcessor()
        self.image_processor = ImageProcessor()
        self.pdf_processor = PDFProcessor()
        self.excel_processor = ExcelProcessor()

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
        """Single source of truth: all Telegram orders go through AI Parser Job API/store."""
        if self.job_store is not None:
            # Prefer job_runner as source of truth
            job, duplicate = runner_create_job(
                self.job_store,
                source="telegram",
                input_type=input_type,
                source_message_id=source_message_id,
                sender_id=sender_id,
                file_name=file_name,
                file_size=file_size,
                input_hash=input_hash or hash_content(str(raw.get("text") or file_name)),
            )
            if duplicate is not None:
                # Duplicate webhook retry — do NOT create second Odoo order
                logger.info("telegram.duplicate_job_blocked", job_id=duplicate.job_id, source_message_id=source_message_id)
                # Return the duplicate's result if completed
                if duplicate.result is not None:
                    return duplicate.result
                return {
                    "status": "review",
                    "job_id": duplicate.job_id,
                    "duplicate_of": duplicate.job_id,
                    "message": "Duplicate Telegram message already ingested",
                }
            # Synchronous execution via job_runner (state machine + metrics)
            # If a queue is available and running, we could enqueue; for Telegram UX we process sync so user gets immediate feedback
            return run_job_sync(self.job_store, self.pipeline, job, parsed, raw=raw)
        # Fallback (tests without job_store): legacy direct pipeline call
        import asyncio

        # pipeline.process is sync; caller already uses to_thread, but keep sync here
        return self.pipeline.process("telegram", input_type, parsed, raw)

    # ------------------------------------------------------------------ helpers

    async def _with_retry(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        attempts: int = 3,
        delay: float = 1.0,
    ) -> Any:
        """Retry Telegram API calls that fail with transient network errors."""
        return await retry_telegram_call(coro_factory, attempts=attempts, delay=delay)

    async def _reply(self, message: Any, text: str, **kwargs: Any) -> None:
        """Reply to a message, tolerating transient Telegram failures."""
        try:
            await self._with_retry(lambda: message.reply_text(text, **kwargs))
        except Exception:
            logger.exception("telegram.reply_failed", text_head=text[:80])

    async def _answer(self, query: Any, text: str | None = None, show_alert: bool = False) -> None:
        """Answer a callback query; expired/invalid queries never crash us."""
        try:
            await self._with_retry(lambda: query.answer(text=text, show_alert=show_alert))
        except Exception as exc:
            logger.warning("telegram.callback_answer_failed", error=str(exc))

    def _identity_from(self, user: Any) -> StaffIdentity:
        if self.staff_registry is not None:
            resolved = self.staff_registry.resolve(getattr(user, "id", None))
            if resolved is not None:
                return resolved
        # Unenforced mode (no allowlist configured): synthesize a dev identity.
        user_id = getattr(user, "id", None) or 0
        name = getattr(user, "full_name", "") or f"user_{user_id}"
        return StaffIdentity(telegram_user_id=user_id, staff_id=f"user_{user_id}", display_name=name)

    @property
    def _auth_enforced(self) -> bool:
        return self.staff_registry is not None and self.staff_registry.enforced

    async def _authorize(self, update: Update) -> StaffIdentity | None:
        """Return the staff identity, or None after rejecting an unknown user."""
        user = update.effective_user
        if self._auth_enforced and self.staff_registry.resolve(getattr(user, "id", None)) is None:
            logger.warning("telegram.unauthorized_user", user_id=getattr(user, "id", None))
            message = update.effective_message
            if message is not None:
                await self._reply(message, "⛔ You are not authorized to use this bot.")
            query = update.callback_query
            if query is not None:
                await query.answer("Not authorized", show_alert=True)
            return None
        return self._identity_from(user)

    @staticmethod
    def _safe_filename(name: str) -> str:
        return Path(name or "upload.bin").name.replace("/", "_").replace("\\", "_") or "upload.bin"

    # -------------------------------------------------------------- entry point

    async def handle_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message:
            return
        try:
            identity = await self._authorize(update)
            if identity is None:
                return

            text = (message.text or "").strip()
            command = text.lower().split(maxsplit=1)[0] if text.startswith("/") else ""

            if self.session_manager is not None:
                if command == "/neworder":
                    await self._cmd_new_order(message, identity)
                    return
                if command == "/done":
                    await self._cmd_done(message, identity)
                    return
                if command == "/cancel":
                    await self._cmd_cancel(message, identity)
                    return
                if command == "/status":
                    await self._cmd_status(message, identity)
                    return

            upper = text.upper()
            if upper.startswith("CONFIRM "):
                order_id = text.split(maxsplit=1)[1].strip()
                result = await asyncio.to_thread(self.pipeline.confirm_order, order_id, "telegram")
                await self._reply(message,format_result(result))
                return
            if upper.startswith("REJECT "):
                order_id = text.split(maxsplit=1)[1].strip()
                result = await asyncio.to_thread(self.pipeline.reject_order, order_id, "telegram")
                await self._reply(message,format_result(result))
                return

            if self.session_manager is not None and not command:
                session = await asyncio.to_thread(self.session_manager.get_collecting_session, identity.staff_id)
                if session is not None:
                    await self._capture_input(message, identity, session)
                    return

            sender_id = str(getattr(message.from_user, "id", "") or "")
            raw: dict[str, Any] = {
                "chat_id": message.chat_id,
                "sender": message.from_user.full_name if message.from_user else "",
            }
            # Use Job API as source of truth — every Telegram input becomes a Job
            source_msg_id = fingerprint_telegram(message.chat_id, message.message_id)
            if message.text:
                parsed = self.text_processor.process(message.text)
                raw["text"] = message.text
                raw["messages"] = [{"text": message.text}]
                result = await asyncio.to_thread(
                    self._run_via_job,
                    source_msg_id,
                    "text",
                    parsed,
                    raw,
                    "",
                    len(message.text.encode("utf-8")),
                    hash_content(message.text),
                    sender_id,
                )
                await self._reply(message,format_result(result))
            elif message.photo:
                photo = message.photo[-1]
                file = await self._with_retry(lambda: photo.get_file())
                data = await self._with_retry(lambda: file.download_as_bytearray())
                data_bytes = bytes(data)
                parsed = self.image_processor.process(data_bytes, filename=f"{photo.file_id}.jpg")
                raw["filename"] = f"{photo.file_id}.jpg"
                raw["caption"] = message.caption or ""
                raw["file_data"] = {"filename": f"{photo.file_id}.jpg", "data": data_bytes}
                if message.caption:
                    raw["messages"] = [{"text": message.caption}]
                result = await asyncio.to_thread(
                    self._run_via_job,
                    source_msg_id,
                    "image",
                    parsed,
                    raw,
                    f"{photo.file_id}.jpg",
                    len(data_bytes),
                    hash_content(data_bytes),
                    sender_id,
                )
                await self._reply(message,format_result(result))
            elif message.document:
                document = message.document
                file = await self._with_retry(lambda: document.get_file())
                data = await self._with_retry(lambda: file.download_as_bytearray())
                data_bytes = bytes(data)
                filename = document.file_name or f"{document.file_id}.bin"
                input_type = detect_input_type(document.mime_type, filename)
                parsed = self._route(input_type, data_bytes, filename)
                raw["filename"] = filename
                raw["caption"] = message.caption or ""
                raw["file_data"] = {"filename": filename, "data": data_bytes}
                if message.caption:
                    raw["messages"] = [{"text": message.caption}]
                result = await asyncio.to_thread(
                    self._run_via_job,
                    source_msg_id,
                    input_type.value,
                    parsed,
                    raw,
                    filename,
                    len(data_bytes),
                    hash_content(data_bytes),
                    sender_id,
                )
                await self._reply(message,format_result(result))
        except Exception as exc:
            logger.exception("telegram.update_failed")
            await self._reply(message,f"Processing failed: {exc}")

    # ---------------------------------------------------------------- callbacks

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or self.session_manager is None:
            return
        try:
            identity = await self._authorize(update)
            if identity is None:
                return
            action = (query.data or "").removeprefix("sess:")
            target = query.message or update.effective_message
            # "why" may carry the exact session id: sess:why:<session_id>
            why_session_id = ""
            if action.startswith("why"):
                parts = action.split(":", 1)
                action = parts[0]
                why_session_id = parts[1] if len(parts) > 1 else ""
            if action not in ("finish", "cancel", "confirm", "correct", "why"):
                await self._answer(query)
                return
            if target is None:
                # Inline-mode / very old messages: no chat surface to act on.
                await self._answer(query, "Original message unavailable.", show_alert=True)
                return
            if action == "finish":
                await self._answer(query)
                await self._finish_and_process(target, identity)
            elif action == "cancel":
                await self._answer(query)
                await self._cancel_active(target, identity)
            elif action == "confirm":
                await self._answer(query)
                await self._confirm_session(target, identity)
            elif action == "why":
                await self._answer(query)
                await self._explain_review_block(target, identity, why_session_id)
            else:  # correct
                await self._answer(query)
                await self._reply(
                    target,
                    "Field-level correction arrives in a later phase. "
                    "Use ✕ Cancel and /neworder to re-enter the order.",
                )
        except Exception as exc:
            metrics.incr("telegram_callbacks_failed_total")
            logger.exception("telegram.callback_failed")
            try:
                await query.answer(f"Action failed: {exc}", show_alert=True)
            except Exception:
                pass

    # ----------------------------------------------------------------- commands

    async def _cmd_new_order(self, message, identity: StaffIdentity) -> None:
        assert self.session_manager is not None
        try:
            session = await asyncio.to_thread(
                self.session_manager.start_session, identity, message.chat_id
            )
        except SessionActiveError as exc:
            await self._reply(message,                f"You already have an active order session ({exc.session.session_id}).\n"
                f"Finish it with /done or cancel it with /cancel.",
                reply_markup=collection_keyboard(),
            )
            return
        await self._reply(message,
            f"🆕 New order started ({session.session_id}).\n"
            f"Send customer details and order information:\n"
            f"text, photos, PDFs or Excel files.",
            reply_markup=collection_keyboard(),
        )

    async def _cmd_done(self, message, identity: StaffIdentity) -> None:
        await self._finish_and_process(message, identity)

    async def _cmd_cancel(self, message, identity: StaffIdentity) -> None:
        await self._cancel_active(message, identity)

    async def _cmd_status(self, message, identity: StaffIdentity) -> None:
        assert self.session_manager is not None
        session = await asyncio.to_thread(
            self.session_manager.get_latest_for_staff, identity.staff_id
        )
        if session is None:
            await self._reply(message, "No active order session. Start one with /neworder.")
            return
        await self._reply(message,
            f"📋 Session {session.session_id}\n"
            f"Status: {session.status.value}\n"
            f"Texts: {len(session.messages)} | Files: {len(session.attachments)}\n"
            f"Started: {session.created_at}",
            reply_markup=collection_keyboard() if session.status == SessionStatus.COLLECTING else None,
        )

    # ------------------------------------------------------------ session flows

    async def _capture_input(self, message, identity: StaffIdentity, session) -> None:
        assert self.session_manager is not None
        if message.text:
            await asyncio.to_thread(
                self.session_manager.add_text,
                session.session_id,
                identity.staff_id,
                message.text,
                message.message_id,
            )
        elif message.photo or message.document:
            payload = await self._download_media(message)
            if payload is None:
                await self._reply(message, "⚠ Could not download this file. Please try again.")
                return
            kind, data, filename, mime, caption = payload
            digest = hashlib.sha256(data).hexdigest()
            store_dir = self.session_manager.store.attachment_dir(session.session_id)
            stored_path = store_dir / f"{digest[:16]}_{self._safe_filename(filename)}"
            stored_path.write_bytes(data)
            attachment = SessionAttachment(
                kind=kind,
                input_type=detect_input_type(mime, filename).value,
                filename=self._safe_filename(filename),
                path=str(stored_path),
                sha256=digest,
                size_bytes=len(data),
                mime_type=mime or "",
                caption=caption or "",
                telegram_message_id=message.message_id,
            )
            try:
                await asyncio.to_thread(
                    self.session_manager.add_attachment,
                    session.session_id,
                    identity.staff_id,
                    attachment,
                )
            except SessionAccessError:
                stored_path.unlink(missing_ok=True)
                raise
        total = len(session.messages) + len(session.attachments)
        await self._reply(message,
            f"✓ Added to your current order ({total} input{'s' if total != 1 else ''}).\n"
            f"Finish with /done or the button.",
            reply_markup=collection_keyboard(),
        )

    async def _download_media(self, message):
        if message.photo:
            photo = message.photo[-1]
            file = await self._with_retry(lambda: photo.get_file())
            data = await self._with_retry(lambda: file.download_as_bytearray())
            return "photo", bytes(data), f"{photo.file_id}.jpg", "image/jpeg", message.caption or ""
        document = message.document
        file = await self._with_retry(lambda: document.get_file())
        data = await self._with_retry(lambda: file.download_as_bytearray())
        filename = document.file_name or f"{document.file_id}.bin"
        return "document", bytes(data), filename, document.mime_type or "", message.caption or ""

    async def _finish_and_process(self, message, identity: StaffIdentity) -> None:
        assert self.session_manager is not None and self.session_service is not None
        session = await asyncio.to_thread(
            self.session_manager.get_collecting_session, identity.staff_id
        )
        if session is None:
            await self._reply(message, "No active order session. Start one with /neworder.")
            return
        try:
            session = await asyncio.to_thread(
                self.session_manager.finish, session.session_id, identity.staff_id
            )
        except SessionError as exc:
            await self._reply(message,f"⚠ {exc}")
            return

        outcome = await asyncio.to_thread(self.session_service.finalize, session)
        final = await asyncio.to_thread(self._apply_outcome, session.session_id, outcome)
        text, reply_kwargs = await asyncio.to_thread(self._outcome_reply_args, outcome, final)
        await self._reply(message, text, **reply_kwargs)

    def _apply_outcome(self, session_id: str, outcome: dict) -> Any:
        """Map the pipeline result onto guarded session transitions."""
        assert self.session_manager is not None
        artifacts = {
            "extracted_fragments": outcome.get("fragments", []),
        }
        if outcome.get("parsed") is not None:
            artifacts["combined_order"] = outcome["parsed"].order.model_dump()
        resolution_summary: dict[str, Any] = {}
        if outcome.get("conflict_warnings"):
            resolution_summary["fragment_conflicts"] = outcome["conflict_warnings"]

        if outcome.get("fatal"):
            artifacts["error_state"] = {"code": "SESSION_EMPTY", "message": outcome["fatal"]}
            self.session_manager.advance(session_id, SessionStatus.FAILED, artifacts)
            return self.session_manager.get(session_id)

        result = outcome.get("result", {})
        status = result.get("status")
        if resolution_summary:
            artifacts["resolution_result"] = resolution_summary

        if status == "success":
            artifacts.update({"odoo_sale_order_name": result.get("sales_order")})
            self.session_manager.advance(session_id, SessionStatus.CREATING_ORDER, artifacts)
            final = self.session_manager.advance(session_id, SessionStatus.COMPLETED)
        elif status == "pending":
            artifacts["confirmation_state"] = {
                "pending_order_id": result.get("order_id"),
                "conflict_warnings": outcome.get("conflict_warnings", []),
            }
            final = self.session_manager.advance(session_id, SessionStatus.WAITING_CONFIRMATION, artifacts)
        elif status == "review":
            artifacts["error_state"] = {
                "code": "REVIEW_REQUIRED",
                "message": "; ".join(result.get("resolution_blocked") or []) or "manual review required",
            }
            final = self.session_manager.advance(session_id, SessionStatus.FAILED, artifacts)
        else:
            artifacts["error_state"] = {
                "code": "PROCESSING_FAILED",
                "message": result.get("message") or "unknown processing failure",
            }
            final = self.session_manager.advance(session_id, SessionStatus.FAILED, artifacts)
        return final

    def _outcome_reply_args(self, outcome: dict, final) -> tuple[str, dict]:
        result = outcome.get("result", {})
        conflicts = outcome.get("conflict_warnings") or []
        conflict_lines = "\n".join(f"⚠ Conflict: {c}" for c in conflicts)
        body = format_result(result)
        if conflict_lines:
            body = f"{body}\n{conflict_lines}"
        if final is not None and final.odoo_sale_order_name:
            header = f"✅ Session {final.session_id} completed.\n"
            args = (header + body, {})
        elif final is not None and final.confirmation_state:
            header = f"🧾 Order ready for confirmation (session {final.session_id}).\n"
            args = (header + body, {"reply_markup": confirmation_keyboard()})
        elif final is not None and final.error_state:
            header = f"⚠ Session {final.session_id} could not be processed.\n"
            args = (header + body, {"reply_markup": review_keyboard(final.session_id)})
        else:
            header = f"⚠ Session {final.session_id if final else ''} could not be processed.\n"
            args = (header + body, {})
        return args

    async def _explain_review_block(
        self, message, identity: StaffIdentity, session_id: str = ""
    ) -> None:
        assert self.session_manager is not None
        try:
            if session_id:
                session = await asyncio.to_thread(self.session_manager.get, session_id)
                if session.staff_id != identity.staff_id:
                    # Do not disclose another staff member's order details.
                    await self._reply(message, "This order belongs to a different staff member.")
                    return
            else:
                session = await asyncio.to_thread(
                    self.session_manager.get_latest_for_staff, identity.staff_id
                )
        except Exception:
            session = None
        error = (getattr(session, "error_state", None) or {}) if session else {}
        raw_reasons = str(error.get("message") or "").split("; ")
        reasons = [r.strip() for r in raw_reasons if r.strip()]
        if not reasons:
            await self._reply(
                message,
                "No blocking details were recorded for that order.\n"
                "Fix the cause in Odoo (product aliases, taxes) and send the "
                "order again with /neworder.",
            )
            return
        bullets = "\n".join(f"  • {reason}" for reason in reasons)
        await self._reply(
            message,
            f"🔎 This order needs manual review. Blocking reasons:\n{bullets}\n\n"
            "Once fixed in Odoo, send the order again with /neworder.",
        )

    async def _cancel_active(self, message, identity: StaffIdentity) -> None:
        assert self.session_manager is not None
        session = await asyncio.to_thread(self.session_manager.get_latest_for_staff, identity.staff_id)
        if session is None:
            await self._reply(message, "No active order session to cancel.")
            return
        try:
            cancelled = await asyncio.to_thread(
                self.session_manager.cancel, session.session_id, identity.staff_id
            )
        except SessionError as exc:
            await self._reply(message,f"⚠ {exc}")
            return
        await self._reply(message,f"✕ Order session {cancelled.session_id} cancelled.")

    async def _confirm_session(self, message, identity: StaffIdentity) -> None:
        assert self.session_manager is not None
        session = await asyncio.to_thread(self.session_manager.get_latest_for_staff, identity.staff_id)
        if session is None or session.status != SessionStatus.WAITING_CONFIRMATION:
            await self._reply(message, "No order awaiting confirmation.")
            return
        pending_id = (session.confirmation_state or {}).get("pending_order_id")
        if not pending_id:
            await self._reply(message, "⚠ Confirmation reference missing; the order was routed for manual review.")
            return
        result = await asyncio.to_thread(self.pipeline.confirm_order, pending_id, "telegram")
        if result.get("status") == "success":
            await asyncio.to_thread(
                self.session_manager.advance, session.session_id, SessionStatus.APPROVED
            )
            await asyncio.to_thread(
                self.session_manager.advance,
                session.session_id,
                SessionStatus.CREATING_ORDER,
                {"odoo_sale_order_name": result.get("sales_order")},
            )
            await asyncio.to_thread(
                self.session_manager.advance, session.session_id, SessionStatus.COMPLETED
            )
            await self._reply(message,f"✅ Confirmed.\n{format_result(result)}")
        else:
            await self._reply(message,f"⚠ Confirmation failed.\n{format_result(result)}")

    def _route(self, input_type: InputType, data: bytes, filename: str):
        if input_type == InputType.PDF:
            return self.pdf_processor.process(data, filename)
        if input_type == InputType.EXCEL:
            return self.excel_processor.process(data, filename)
        if input_type == InputType.IMAGE:
            return self.image_processor.process(data, filename)
        return self.text_processor.process(data.decode("utf-8", errors="replace"))
