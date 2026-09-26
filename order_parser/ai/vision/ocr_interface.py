from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OCRResult:
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class OCRProvider(ABC):
    """Replaceable OCR abstraction. Providers implement extract_text().

    The parser never couples to a single vendor; switching providers is a
    configuration change only.
    """

    provider_name: str = "abstract"

    @property
    @abstractmethod
    def configured(self) -> bool:
        """Whether this provider is ready to be called."""

    @abstractmethod
    def extract_text(self, image_bytes: bytes, mime_type: str = "image/png") -> OCRResult:
        """Extract raw text from image bytes."""


class NoopOCRProvider(OCRProvider):
    """Disabled OCR — never configured, used as safe fallback."""

    provider_name = "noop"

    @property
    def configured(self) -> bool:
        return False

    def extract_text(self, image_bytes: bytes, mime_type: str = "image/png") -> OCRResult:
        raise RuntimeError("OCR not configured")
