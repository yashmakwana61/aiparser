from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

import structlog

from order_parser.ai.prompts import VISION_PROMPT
from order_parser.ai.puter import puter_chat
from order_parser.config import get_settings
from order_parser.core import metrics

logger = structlog.get_logger(__name__)

# Test seam: monkeypatched to avoid real sleeping in unit tests.
_sleep = time.sleep


class VisionParser:
    """DEPRECATED: GPT-vision direct image parsing.

    Kept for backward compatibility / rollback only. Production image and
    scanned-PDF flows now use Google Vision OCR exclusively
    (``VisionOCRService`` + ``TextParser``); processors no longer call this
    class. Calls Puter's AI gateway via the free /drivers/call endpoint with images
    (model: ai_vision_model).

    Images are sent as data-URL image_url parts in the standard OpenAI
    multimodal format, which Puter's chat driver accepts.
    """

    def __init__(self, client: Any | None = None):
        settings = get_settings()
        self._client = client
        self.auth_token = settings.puter_auth_token
        self.model = settings.ai_vision_model
        self.max_attempts = max(1, int(getattr(settings, "ai_max_attempts", 3)))
        self.backoff_seconds = float(getattr(settings, "ai_retry_backoff_seconds", 2.0))

    def _transport(self, args: dict[str, Any]) -> str:
        if self._client is not None:
            return self._client(args)
        return puter_chat(args)

    def parse(self, images: list[bytes], filename: str = "order_image.png") -> dict[str, Any]:
        mime = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
        content: list[dict[str, Any]] = [{"type": "text", "text": VISION_PROMPT}]
        for image_bytes in images:
            encoded = base64.b64encode(image_bytes).decode("utf-8")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{encoded}",
                        "detail": "high",
                    },
                }
            )
        args = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 2000,
        }
        last_exc: ValueError | None = None
        for attempt in range(1, self.max_attempts + 1):
            raw = self._transport(args)
            try:
                return self._clean_json(raw)
            except ValueError as exc:
                last_exc = exc
                metrics.incr("ai_api_retries_total", outcome="invalid_json")
                logger.warning("ai.invalid_json_retry", model=self.model, attempt=attempt)
                if attempt < self.max_attempts:
                    _sleep(self.backoff_seconds * (attempt - 1))
        raise ValueError(
            f"Vision AI response was not valid JSON after {self.max_attempts} attempts."
        ) from last_exc

    @staticmethod
    def _clean_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        raise ValueError("Vision AI response was not valid JSON.")