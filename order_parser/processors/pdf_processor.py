from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from order_parser.ai.text_parser import TextParser
from order_parser.ai.vision.ocr_service import OCRError, VisionOCRService
from order_parser.ai.vision_parser import VisionParser
from order_parser.config import get_settings
from order_parser.core.attachment_store import AttachmentStore
from order_parser.extractors.pdf_extractor import PDFExtractor
from order_parser.models import ParsedOrder
from order_parser.normalizers.order_normalizer import OrderNormalizer

MIN_TEXT_CHARS = 30

logger = structlog.get_logger(__name__)


class PDFProcessor:
    """Production PDF flow (Phase 4).

    Text PDFs go to the text parser (never OCR'd blindly). Scanned/image
    PDFs are rendered per page, OCR'd with Google Vision, combined and sent
    to the AI interpretation layer. The original PDF is preserved and
    referenced by hash. When Google Vision is not configured the previous
    direct AI-vision fallback is kept unchanged. OCR failures NEVER create
    orders - a flagged ``ocr_failed`` ParsedOrder routes them to review.
    """

    def __init__(
        self,
        text_parser: TextParser | None = None,
        vision_parser: VisionParser | None = None,
        ocr_service: VisionOCRService | None = None,
        attachment_store: AttachmentStore | None = None,
    ):
        self.text_parser = text_parser or TextParser()
        self.vision_parser = vision_parser or VisionParser()
        self.ocr_service = ocr_service  # lazily built default when None
        self.store = attachment_store

    # ------------------------------------------------------------------ public

    def process(self, data: bytes, filename: str = "order.pdf") -> ParsedOrder:
        settings = get_settings()
        store = self.store or AttachmentStore()
        ref = store.save(data, Path(filename).suffix or ".pdf")
        attachment_meta: dict[str, Any] = {
            "kind": "pdf",
            "filename": filename,
            "sha256": ref["sha256"],
            "path": ref["path"],
            "size_bytes": ref["size_bytes"],
        }

        text = PDFExtractor.extract_text(data)
        page_count = PDFExtractor.page_count(data)
        min_total = max(MIN_TEXT_CHARS, page_count * int(settings.pdf_min_chars_per_page))

        if len(text.strip()) >= min_total:
            ai_response = self.text_parser.parse(text)
            ai_response["attachment"] = attachment_meta
            return ParsedOrder(
                order=OrderNormalizer.normalize(ai_response, "", "pdf"),
                ai_response=ai_response,
                extracted_text=text,
            )

        pages = PDFExtractor.render_pages(data)
        ocr = self.ocr_service or VisionOCRService()
        if not ocr.enabled:
            ai_response = self.vision_parser.parse(pages, filename=filename)
            ai_response["attachment"] = attachment_meta
            return ParsedOrder(
                order=OrderNormalizer.normalize(ai_response, "", "pdf_image"),
                ai_response=ai_response,
            )

        page_texts: list[str] = []
        page_meta: list[dict[str, Any]] = []
        try:
            for index, png in enumerate(pages, start=1):
                result = ocr.extract_text(png, filename=f"{filename}#p{index}", mime_type="image/png")
                page_texts.append(result.text)
                page_meta.append({"page": index, **result.metadata})
        except OCRError as exc:
            logger.warning("vision.ocr_failed", filename=filename, error=str(exc))
            return self._failed(filename, "OCR_UNAVAILABLE", str(exc), attachment_meta)

        combined = "\n\n".join(t for t in page_texts if t).strip()
        attachment_meta["ocr"] = {"provider": ocr.provider, "pages": page_meta}
        if not combined:
            return self._failed(filename, "OCR_EMPTY", "no text detected in any page", attachment_meta)
        try:
            ai_response = self.text_parser.parse(combined)
        except Exception as exc:
            logger.warning("vision.interpretation_failed", filename=filename, error=str(exc))
            return self._failed(filename, "AI_INTERPRETATION_FAILED", str(exc), attachment_meta)
        ai_response["attachment"] = attachment_meta
        return ParsedOrder(
            order=OrderNormalizer.normalize(ai_response, "", "pdf_scanned"),
            ai_response=ai_response,
            extracted_text=combined,
        )

    @staticmethod
    def _failed(
        filename: str,
        code: str,
        detail: str,
        attachment_meta: dict[str, Any] | None = None,
    ) -> ParsedOrder:
        ai_response: dict[str, Any] = {
            "customer": {"name": ""},
            "items": [],
            "notes": f"{code}: {detail}",
            "confidence": 0.0,
            "ocr_failed": True,
            "error_code": code,
            "error_detail": detail,
        }
        if attachment_meta:
            ai_response["attachment"] = attachment_meta
        logger.warning("vision.process_flagged_for_review", filename=filename, code=code)
        return ParsedOrder(order=OrderNormalizer.normalize(ai_response, "", "pdf"), ai_response=ai_response)
