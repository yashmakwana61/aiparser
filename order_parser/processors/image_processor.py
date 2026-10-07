from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from order_parser.ai.text_parser import TextParser, classify_ai_failure
from order_parser.ai.vision.ocr_service import OCRError, VisionOCRService
from order_parser.config import get_settings
from order_parser.core.attachment_store import AttachmentStore
from order_parser.models import ParsedOrder
from order_parser.normalizers.order_normalizer import OrderNormalizer

logger = structlog.get_logger(__name__)

# Magic-number sniffing: file type validation never trusts the extension alone.
IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)


def sniff_image_mime(data: bytes, filename: str = "") -> str | None:
    for signature, mime in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12].lower() in (b"heic", b"heix", b"mif1"):
        return "image/heic"
    return None


class ImageProcessor:
    """Production image flow (Google Vision OCR only):

    validate type -> validate size -> store original -> SHA-256 -> Google
    Vision OCR -> raw text -> AI interpretation -> OrderModel. The original
    image is always preserved and referenced by hash.

    GPT-vision direct fallback has been removed. When Google Vision is not
    configured (missing API key) or OCR fails, a flagged ``ocr_failed``
    ParsedOrder is returned so the pipeline routes it to review - orders
    are NEVER created from OCR failures.
    """

    def __init__(
        self,
        parser: Any | None = None,
        ocr_service: VisionOCRService | None = None,
        text_parser: TextParser | None = None,
        attachment_store: AttachmentStore | None = None,
    ):
        # ``parser`` (legacy GPT VisionParser) is accepted for backward
        # compatibility but ignored: OCR is now Google Vision only.
        self.parser = parser
        self.text_parser = text_parser or TextParser()
        self.ocr_service = ocr_service  # lazily built default when None
        self.store = attachment_store

    # ------------------------------------------------------------------ public

    def process(self, image_bytes: bytes, filename: str = "order_image.png") -> ParsedOrder:
        settings = get_settings()
        mime = sniff_image_mime(image_bytes, filename)
        if mime is None:
            return self._failed(filename, "UNSUPPORTED_IMAGE_TYPE", "file is not a recognized image")
        max_bytes = max(0, int(settings.max_upload_mb)) * 1024 * 1024
        if len(image_bytes) > max_bytes:
            return self._failed(
                filename,
                "IMAGE_TOO_LARGE",
                f"{len(image_bytes)} bytes exceeds the {settings.max_upload_mb} MB limit",
            )

        attachment_meta, _ = self._preserve(image_bytes, filename)
        attachment_meta["mime_type"] = mime
        ocr = self.ocr_service or VisionOCRService()

        if not ocr.enabled:
            logger.warning("vision.ocr_not_configured", filename=filename)
            return self._failed(
                filename,
                "OCR_UNAVAILABLE",
                "Google Vision OCR not configured (GOOGLE_VISION_API_KEY)",
                attachment_meta,
            )
        try:
            result = ocr.extract_text(image_bytes, filename=filename, mime_type=mime)
        except OCRError as exc:
            logger.warning("vision.ocr_failed", filename=filename, error=str(exc))
            return self._failed(filename, "OCR_UNAVAILABLE", str(exc), attachment_meta)
        attachment_meta["ocr"] = result.metadata
        text = result.text
        if not text:
            return self._failed(filename, "OCR_EMPTY", "no text detected in image", attachment_meta)
        try:
            ai_response = self.text_parser.parse(text)
        except Exception as exc:
            logger.warning("vision.interpretation_failed", filename=filename, error=str(exc))
            return self._failed(filename, classify_ai_failure(exc), str(exc), attachment_meta)
        ai_response["attachment"] = attachment_meta
        order = OrderNormalizer.normalize(ai_response, source="", input_type="image")
        return ParsedOrder(order=order, ai_response=ai_response, extracted_text=text)

    # ---------------------------------------------------------------- internals

    def _preserve(self, data: bytes, filename: str) -> tuple[dict[str, Any], Any]:
        store = self.store or AttachmentStore()
        suffix = Path(filename).suffix or ".png"
        ref = store.save(data, suffix)
        meta: dict[str, Any] = {
            "kind": "image",
            "filename": filename,
            "sha256": ref["sha256"],
            "path": ref["path"],
            "size_bytes": ref["size_bytes"],
        }
        return meta, ref

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
        order = OrderNormalizer.normalize(ai_response, source="", input_type="image")
        logger.warning("vision.process_flagged_for_review", filename=filename, code=code)
        return ParsedOrder(order=order, ai_response=ai_response)
