from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from order_parser.ai.vision.google_vision_client import GoogleVisionClient, GoogleVisionError
from order_parser.ai.vision.ocr_interface import OCRProvider, OCRResult as InterfaceOCRResult

logger = structlog.get_logger(__name__)

OCRError = GoogleVisionError


@dataclass
class OCRResult:
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class VisionOCRService(OCRProvider):
    """OCR layer: image bytes in, RAW TEXT out. No order understanding here.

    Implements :class:`OCRProvider` so the parser depends on the interface,
    not a single vendor. ``provider_name`` identifies the current backend.
    The service is ``enabled`` only when the underlying client is configured;
    processors fall back to their previous behavior when it is not.
    """

    provider = "google_vision"
    provider_name = "google_vision"

    def __init__(self, client: GoogleVisionClient | None = None) -> None:
        self.client = client or GoogleVisionClient()

    @property
    def configured(self) -> bool:
        return self.client.configured

    @property
    def enabled(self) -> bool:
        return self.client.configured

    def extract_text(self, image_bytes: bytes, filename: str = "", mime_type: str = "image/png") -> OCRResult:
        response = self.client.annotate(image_bytes, mime_type=mime_type)
        responses = response.get("responses") or [{}]
        annotation = (responses[0] or {}).get("fullTextAnnotation") or {}
        text = str(annotation.get("text") or "").strip()
        pages = annotation.get("pages") or []
        confidences = [
            float(page["confidence"])
            for page in pages
            if isinstance(page, dict) and isinstance(page.get("confidence"), (int, float))
        ]
        average = round(sum(confidences) / len(confidences) * 100, 1) if confidences else None
        metadata = {
            "provider": self.provider,
            "pages": len(pages),
            "confidence": average,
            "filename": filename,
        }
        logger.info(
            "vision.ocr_completed",
            provider=self.provider,
            pages=metadata["pages"],
            confidence=average,
            chars=len(text),
        )
        return OCRResult(text=text, metadata=metadata)
