from __future__ import annotations

"""Semantic order extraction via the Puter AI gateway (ChatGPT text model).

Production text path: already-extracted TEXT in (from typed input, PDF text
layer, Excel fallback, or Google Vision OCR for images/scanned PDFs),
structured order JSON out. OCR is handled separately by Google Vision; this
parser never sees images and never performs OCR.
"""

import json
import re
import time
from typing import Any

import structlog

from order_parser.ai.prompts import TEXT_PROMPT
from order_parser.ai.puter import puter_chat
from order_parser.config import get_settings
from order_parser.core import metrics

logger = structlog.get_logger(__name__)

# Test seam: monkeypatched to avoid real sleeping in unit tests.
_sleep = time.sleep


class TextParser:
    """Calls Puter's AI gateway via the free /drivers/call endpoint
    (model: ai_text_model, e.g. gpt-4.1) to normalize order text."""

    def __init__(self, client: Any | None = None):
        settings = get_settings()
        self._client = client
        self.auth_token = settings.puter_auth_token
        self.model = settings.ai_text_model
        self.max_attempts = max(1, int(getattr(settings, "ai_max_attempts", 3)))
        self.backoff_seconds = float(getattr(settings, "ai_retry_backoff_seconds", 2.0))

    def _transport(self, args: dict[str, Any]) -> str:
        if self._client is not None:
            return self._client(args)
        return puter_chat(args)

    def parse(self, content: str) -> dict[str, Any]:
        prompt = TEXT_PROMPT.replace("{{CONTENT}}", content[:30000])
        args = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 2000,
        }
        last_exc: ValueError | None = None
        for attempt in range(1, self.max_attempts + 1):
            raw = self._transport(args)
            try:
                return self._clean_json(raw)
            except ValueError as exc:
                # LLMs occasionally wrap or truncate JSON; a fresh attempt
                # usually resolves it. Transport/network retries live in
                # the gateway client, not here.
                last_exc = exc
                metrics.incr("ai_api_retries_total", outcome="invalid_json")
                logger.warning("ai.invalid_json_retry", model=self.model, attempt=attempt)
                if attempt < self.max_attempts:
                    _sleep(self.backoff_seconds * (attempt - 1))
        raise ValueError(f"AI response was not valid JSON after {self.max_attempts} attempts.") from last_exc

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
        raise ValueError("AI response was not valid JSON.")
