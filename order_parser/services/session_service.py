from __future__ import annotations

from typing import Any

import structlog

from order_parser.core.job_runner import create_job as runner_create_job
from order_parser.core.job_runner import hash_content, run_job_sync
from order_parser.models import ParsedOrder
from order_parser.processors.detector import InputType, detect_input_type
from order_parser.processors.excel_processor import ExcelProcessor
from order_parser.processors.image_processor import ImageProcessor
from order_parser.processors.pdf_processor import PDFProcessor
from order_parser.processors.text_processor import TextProcessor
from order_parser.services.aggregation import AggregationEntry, OrderAggregator
from order_parser.sessions.models import StaffSession

logger = structlog.get_logger(__name__)


class SessionService:
    """Turns a collected staff session into exactly one processed order.

    Every fragment (text message, image, PDF, Excel sheet) is extracted with
    the existing processors and merged by the Phase 5 aggregation layer with
    full field provenance (source / confidence / timestamp / priority / rule).
    The merged order is handed to the existing :class:`OrderPipeline` once —
    master data resolution, business validation, decision gating and Odoo
    execution are unchanged.

    Material conflicts (quantity / UOM / price / different customers) force
    the confirmation flow so staff explicitly approve the final numbers.
    Nothing is ever chosen silently.
    """

    def __init__(self, pipeline, processors: Any | None = None, aggregator: OrderAggregator | None = None, job_store=None) -> None:
        self.pipeline = pipeline
        self.job_store = job_store
        self.aggregator = aggregator or OrderAggregator()
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

    # ------------------------------------------------------------------ public

    def run(self, session: StaffSession) -> dict[str, Any]:
        """Extract and merge all fragments without touching the pipeline."""
        entries: list[AggregationEntry] = []
        warnings: list[str] = []

        def add(label: str, parsed: ParsedOrder, kind: str, timestamp: str = "") -> None:
            entries.append(AggregationEntry(label=label, parsed=parsed, kind=kind, timestamp=timestamp))

        for index, message in enumerate(session.messages, start=1):
            label = f"text#{index}"
            try:
                add(label, self.text_processor.process(message.text), kind="text", timestamp=message.received_at)
            except Exception as exc:
                logger.warning(
                    "session.fragment_failed",
                    session_id=session.session_id,
                    source=label,
                    error=str(exc),
                )
                warnings.append(f"{label} could not be parsed: {exc}")

        for attachment in session.attachments:
            try:
                data = open(attachment.path, "rb").read()
            except OSError as exc:
                logger.warning("session.attachment_unreadable", session_id=session.session_id, path=attachment.path)
                warnings.append(f"attachment '{attachment.filename}' could not be read: {exc}")
                continue
            try:
                parsed = self._route(attachment.input_type, data, attachment.filename)
                label = f"{attachment.input_type}:{attachment.filename}"
                add(label, parsed, kind=attachment.input_type, timestamp=attachment.received_at)
            except Exception as exc:
                logger.warning(
                    "session.fragment_failed",
                    session_id=session.session_id,
                    filename=attachment.filename,
                    error=str(exc),
                )
                warnings.append(f"attachment '{attachment.filename}' could not be parsed: {exc}")
            if attachment.caption.strip():
                try:
                    add(
                        f"caption:{attachment.filename}",
                        self.text_processor.process(attachment.caption),
                        kind="caption",
                        timestamp=attachment.received_at,
                    )
                except Exception:
                    pass

        if not entries:
            return {
                "parsed": None,
                "fragments": [],
                "conflict_warnings": [],
                "forced_confirmation": False,
                "fatal": "No usable order content was found in this session.",
            }

        result = self.aggregator.aggregate(entries)
        merged = result.parsed
        conflict_messages = result.conflict_messages

        if result.forced_confirmation:
            # Deterministic downgrade: material conflicts may never be
            # auto-created; staff must confirm explicitly.
            cap = float(self.pipeline.settings.auto_create_threshold) - 1.0
            merged.order.metadata.confidence = min(merged.order.metadata.confidence, cap)

        all_notes = warnings + [f"conflict: {m}" for m in conflict_messages]
        if all_notes:
            note = "; ".join(all_notes)
            merged.order.metadata.notes = (
                f"{merged.order.metadata.notes}; {note}" if merged.order.metadata.notes else note
            )

        merged.ai_response = {"aggregation": result.provenance()}

        summaries = [
            {
                "source": entry.label,
                "customer": entry.parsed.order.customer.name,
                "items": len(entry.parsed.order.items),
                "products": [item.product_name for item in entry.parsed.order.items],
            }
            for entry in entries
        ]
        return {
            "parsed": merged,
            "fragments": summaries,
            "conflict_warnings": conflict_messages,
            "conflicts": [c.as_dict() for c in result.conflicts],
            "provenance": result.provenance(),
            "forced_confirmation": result.forced_confirmation,
            "fatal": None,
        }

    def finalize(self, session: StaffSession) -> dict[str, Any]:
        """Extract, merge and process the session through the pipeline once — via Job API as source of truth."""
        outcome = self.run(session)
        if outcome["fatal"]:
            return outcome

        texts = [m.text for m in session.messages]
        messages: list[dict[str, str]] = [{"text": m.text} for m in session.messages]
        for att in session.attachments:
            if att.caption and att.caption.strip():
                messages.append({"text": att.caption})
        raw: dict[str, Any] = {
            "chat_id": session.chat_id,
            "sender": session.staff_name or session.staff_id,
            "session_id": session.session_id,
            "text": "\n".join(texts)[:20000],
            "messages": messages,
        }
        if session.attachments:
            first_att = session.attachments[0]
            try:
                att_data = open(first_att.path, "rb").read()
                raw["file_data"] = {"filename": first_att.filename, "data": att_data}
            except OSError:
                pass
        # Use Job API as source of truth for session orders (idempotency via session_id)
        if self.job_store is not None:
            input_hash = hash_content(raw.get("text", "") + session.session_id)
            job, duplicate = runner_create_job(
                self.job_store,
                source="telegram",
                input_type="session",
                source_message_id=f"session:{session.session_id}",
                sender_id=session.staff_id,
                file_name="",
                file_size=len(raw.get("text", "").encode("utf-8")),
                input_hash=input_hash,
            )
            if duplicate is not None:
                outcome["result"] = duplicate.result or {"status": "review", "job_id": duplicate.job_id, "duplicate_of": duplicate.job_id, "message": "Duplicate session already processed"}
                outcome["job_id"] = duplicate.job_id
                return outcome
            result = run_job_sync(self.job_store, self.pipeline, job, outcome["parsed"], raw=raw)
            outcome["result"] = result
            outcome["job_id"] = job.job_id
            return outcome
        result = self.pipeline.process("telegram", "session", outcome["parsed"], raw)
        outcome["result"] = result
        return outcome

    def _route(self, input_type: str, data: bytes, filename: str) -> ParsedOrder:
        kind = InputType(input_type) if input_type in InputType._value2member_map_ else detect_input_type(None, filename)
        if kind == InputType.PDF:
            return self.pdf_processor.process(data, filename)
        if kind == InputType.EXCEL:
            return self.excel_processor.process(data, filename)
        if kind == InputType.IMAGE:
            return self.image_processor.process(data, filename)
        return self.text_processor.process(data.decode("utf-8", errors="replace"))
