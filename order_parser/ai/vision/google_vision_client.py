from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

import structlog

from order_parser.config import get_settings

logger = structlog.get_logger(__name__)

# Transport signature: (url, body_json, timeout_seconds) -> (http_status, body_text)
Transport = Callable[[str, str, float], tuple[int, str]]


class GoogleVisionError(Exception):
    """Raised when Google Vision OCR cannot produce a result.

    ``retryable`` marks transient conditions (network, timeout, 429, 5xx);
    callers must never create orders on any GoogleVisionError.
    """

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class GoogleVisionClient:
    """Thin REST adapter for the Google Cloud Vision API.

    Credentials are read from configuration only (``google_vision_api_key``);
    they are never hard-coded and never logged. Implements timeout, retry
    with exponential backoff and rate-limit handling.
    """

    ANNOTATE_PATH = "/v1/images:annotate"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
        language_hints: list[str] | None = None,
        transport: Transport | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = settings.google_vision_api_key if api_key is None else api_key
        self.base_url = (settings.google_vision_base_url if base_url is None else base_url).rstrip("/")
        self.timeout_seconds = float(settings.google_vision_timeout_seconds if timeout_seconds is None else timeout_seconds)
        self.max_retries = int(settings.google_vision_max_retries if max_retries is None else max_retries)
        self.backoff_seconds = float(
            settings.google_vision_backoff_seconds if backoff_seconds is None else backoff_seconds
        )
        self.language_hints = language_hints or ["en"]
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------ public

    def annotate(self, image_bytes: bytes, mime_type: str = "image/png") -> dict[str, Any]:
        if not self.configured:
            raise GoogleVisionError("google_vision_not_configured")
        request = {
            "requests": [
                {
                    "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                    "imageContext": {"languageHints": self.language_hints},
                }
            ]
        }
        url = f"{self.base_url}{self.ANNOTATE_PATH}?key={self.api_key}"
        return self._execute(url, json.dumps(request))

    # ----------------------------------------------------------------- internals

    def _execute(self, url: str, body: str) -> dict[str, Any]:
        attempt = 0
        last_error: GoogleVisionError | None = None
        while attempt <= self.max_retries:
            try:
                status, raw = self._send(url, body, self.timeout_seconds)
            except Exception as exc:  # network/timeout -> transient
                last_error = GoogleVisionError(f"vision_transport_error: {exc}", retryable=True)
                status, raw = None, ""
            if status is not None and 200 <= status < 300:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise GoogleVisionError("vision_invalid_response_json") from exc
                error = parsed.get("error") if isinstance(parsed, dict) else None
                if error:
                    raise GoogleVisionError(str(error.get("message", "vision_error")), status=error.get("code"))
                return parsed
            if status is not None and status not in (429,) and not (500 <= status < 600):
                raise GoogleVisionError(f"vision_http_{status}", status=status, retryable=False)
            last_error = GoogleVisionError(
                f"vision_http_{status}" if status is not None else (last_error.message if last_error else "vision_error"),
                status=status,
                retryable=True,
            )
            if attempt < self.max_retries:
                delay = self.backoff_seconds * (2**attempt)
                logger.warning(
                    "vision.retry",
                    attempt=attempt + 1,
                    delay=delay,
                    http_status=status,
                )
                time.sleep(delay)
            attempt += 1
        raise last_error or GoogleVisionError("vision_failed")

    def _send(self, url: str, body: str, timeout: float) -> tuple[int, str]:
        if self._transport is not None:
            return self._transport(url, body, timeout)
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.getcode(), response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")
