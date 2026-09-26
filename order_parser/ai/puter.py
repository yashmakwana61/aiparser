from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

import structlog

from order_parser.config import get_settings
from order_parser.core import metrics
from order_parser.core.breaker import CircuitOpenError, get_dependency_breaker

logger = structlog.get_logger(__name__)

# HTTP statuses worth a second attempt; anything else (401/403/400...) is
# deterministic and fails immediately.
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}

# Test seam: monkeypatched to avoid real sleeping in unit tests.
_sleep = time.sleep


class PuterError(RuntimeError):
    """Raised when the Puter AI gateway call fails."""


class TransientAIError(PuterError):
    """Retryable failure (network blip, retryable status, malformed body)."""


def _extract_content(body: dict[str, Any]) -> str:
    result = body.get("result") or {}
    message = result.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                parts.append(part["text"])
        return "".join(parts)
    raise PuterError("Puter AI returned no message content.")


def _call_once(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code in RETRYABLE_HTTP_STATUS:
            raise TransientAIError(f"HTTP {exc.code}: {detail[:200]}") from exc
        raise PuterError(f"Puter AI request failed (HTTP {exc.code}): {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise TransientAIError(f"network error: {exc.reason}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TransientAIError(f"malformed response body: {exc}") from exc


def puter_chat(args: dict[str, Any]) -> str:
    """Call Puter's AI gateway via the driver interface.

    The OpenAI-/Anthropic-compatible ``/puterai/*`` endpoints require a paid
    plan; ``/drivers/call`` is available to free accounts under the standard
    usage quotas. Transient failures are retried with linear backoff;
    application-level answers (auth errors, empty content) fail immediately.
    """
    settings = get_settings()
    token = settings.puter_auth_token
    if not token:
        raise PuterError(
            "PUTER_AUTH_TOKEN is not configured. Create a token at "
            "https://puter.com/dashboard and set it in .env"
        )

    max_attempts = max(1, int(getattr(settings, "ai_max_attempts", 3)))
    backoff = float(getattr(settings, "ai_retry_backoff_seconds", 2.0))
    timeout = float(getattr(settings, "ai_timeout_seconds", 120.0))

    breaker = get_dependency_breaker("ai_gateway", settings)
    if breaker is not None and not breaker.allow():
        logger.warning("ai.circuit_open_fail_fast")
        raise CircuitOpenError("AI gateway circuit open; request rejected without network attempt")

    payload = {
        "interface": "puter-chat-completion",
        "method": "complete",
        "test_mode": False,
        "args": args,
    }
    request = urllib.request.Request(
        settings.ai_drivers_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    last_exc: TransientAIError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            body = _call_once(request, timeout)
        except TransientAIError as exc:
            last_exc = exc
            metrics.incr("ai_api_retries_total", outcome="transient")
            logger.warning(
                "ai.transient_failure",
                attempt=attempt,
                max_attempts=max_attempts,
                error=str(exc),
            )
            if attempt < max_attempts:
                _sleep(backoff * (attempt - 1))
            continue
        if breaker is not None:
            breaker.record_success()
        return _extract_content(body)
    metrics.incr("ai_api_retries_total", outcome="exhausted")
    if breaker is not None:
        breaker.record_failure()
    assert last_exc is not None
    raise PuterError(
        f"Puter AI unavailable after {max_attempts} attempts: {last_exc}"
    ) from last_exc
