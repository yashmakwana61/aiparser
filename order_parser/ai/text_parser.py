from __future__ import annotations

"""Local semantic order extraction (NuExtract via Ollama).

Architecture note — OCR and semantic extraction are separate layers:

* Google Vision (``VisionOCRService``): OCR only — image bytes in, raw text out.
* NuExtract-tiny-v1.5 via the local Ollama server: semantic order extraction
  only — already-extracted TEXT in, structured order JSON out. It never sees
  images and never performs OCR.

No order text is sent to any external LLM (Puter/GPT/OpenAI/...) on the
production extraction path. When Ollama/NuExtract is unavailable or returns
unusable output, a controlled :class:`NuExtractError` is raised so the
existing job/error/review pipeline can mark the job as failed/review-required
— an empty extraction and an infrastructure failure are different states and
are never conflated into fabricated order data.
"""

import json
import queue
import re
import threading
import time
from typing import Any, Callable, Dict

import ollama
import structlog

from order_parser.config import get_settings
from order_parser.core import metrics
from order_parser.core.breaker import CircuitOpenError, get_dependency_breaker

logger = structlog.get_logger(__name__)

# Test seam: monkeypatched to avoid real sleeping in unit tests.
_sleep = time.sleep

# Deterministic extraction, as recommended upstream for pure extraction tasks
# (NuExtract must run at or very near temperature 0). Temperature alone does
# not guarantee well-formed output — production safety still comes from the
# num_predict cap, stop sequences, Pydantic validation, deterministic
# normalization, customer/product resolution, confidence rules, missing-field
# handling and readiness checks downstream.
NUEXTRACT_TEMPERATURE = 0.0

# Stop sequences: NuExtract terminates the filled template with
# ``<|end-output|>``; the remaining entries mirror the model Modelfile so a
# stray role token also ends generation instead of rambling.
NUEXTRACT_STOPS = ("<|end-output|>", "<|end|>", "<|assistant|>", "<|user|>")

# Endpoints already served by the SDK's shared module client. An explicit
# host-bound client is built only for non-default endpoints (e.g.
# host.docker.internal from inside Docker); this keeps the default local path
# on ``ollama.generate`` (also the unit-test seam) while honouring any
# configured topology.
_SDK_DEFAULT_HOSTS = frozenset(
    {
        "http://127.0.0.1:11434",
        "http://localhost:11434",
    }
)


class NuExtractError(RuntimeError):
    """Controlled NuExtract/Ollama failure.

    ``code`` is one of ``NUEXTRACT_EMPTY_INPUT``, ``NUEXTRACT_UNAVAILABLE``,
    ``NUEXTRACT_TIMEOUT``, ``NUEXTRACT_INVALID_JSON`` or
    ``NUEXTRACT_INVALID_SCHEMA``. The code is part of the message so the outer
    job system can classify the failure without importing this module.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# ------------------------------------------------------------------ template


def build_nuextract_schema() -> Dict[str, Any]:
    """Canonical NuExtract extraction template (normalizer-compatible).

    Field names deliberately match what ``OrderNormalizer`` expects
    (``customer.name``, ``items[].product_name``/``quantity``/``unit_price``/
    ``uom``, ``confidence``, ``missing_fields``). The model extracts raw
    values only — it never decides Odoo customer/product IDs.
    """
    return {
        "customer": {
            "name": "",
            "email": "",
            "phone": "",
            "address": "",
            "city": "",
            "state": "",
            "zip_code": "",
            "gstin": "",
            "country": "",
        },
        "items": [
            {
                "product_name": "",
                "quantity": 0,
                "unit_price": None,
                "uom": "",
                "ambiguous": False,
            }
        ],
        "order_reference": "",
        "order_date": "",
        "delivery_date": "",
        "billing_address": "",
        "shipping_address": "",
        "currency": "",
        "notes": "",
        "confidence": 0,
        "missing_fields": [],
    }


def build_nuextract_prompt(raw_text: str) -> str:
    """Official NuExtract prompt format (per the Numind model card).

    ``<|input|>`` + ``### Template:`` + JSON schema + ``### Text:`` + order
    text + ``<|output|>``. Verified on the production host: the bare
    ``Template:``/``Text:`` shape without markers sends this model family
    into prompt-echo loops instead of extracting.
    """
    return (
        "<|input|>\n### Template:\n"
        f"{json.dumps(build_nuextract_schema())}"
        f"\n\n### Text:\n{raw_text}\n<|output|>\n"
    )


def build_nuextract_options(settings) -> Dict[str, Any]:
    """Ollama generation options for faithful, bounded extraction."""
    return {
        "temperature": NUEXTRACT_TEMPERATURE,
        "num_predict": max(128, int(getattr(settings, "nuextract_num_predict", 800))),
        "num_ctx": max(2048, int(getattr(settings, "nuextract_num_ctx", 4096))),
        "stop": list(NUEXTRACT_STOPS),
    }


# --------------------------------------------------------------- json utils


def _clean_json(raw: str) -> Dict[str, Any]:
    """Defensively decode model output as JSON (``json.loads`` only).

    Supports direct JSON, surrounding whitespace, fenced ```json blocks,
    leading explanatory preamble, and repeated JSON blocks (the model
    sometimes re-emits the filled template): the first complete balanced
    ``{...}`` object that decodes to a dict wins. Raises
    :class:`NuExtractError` (``NUEXTRACT_INVALID_JSON`` /
    ``NUEXTRACT_INVALID_SCHEMA``) when nothing usable can be recovered.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise NuExtractError(
            "model output decoded to a non-object JSON value.",
            code="NUEXTRACT_INVALID_SCHEMA",
        )
    except json.JSONDecodeError:
        pass
    # First balanced object that decodes (handles preamble + repetitions).
    first_error: json.JSONDecodeError | None = None
    for match in re.finditer(r"\{", text):
        depth = 0
        in_string = False
        escaped = False
        for index in range(match.start(), len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[match.start() : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError as exc:
                        first_error = exc
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
    if first_error is not None:
        raise NuExtractError(
            "model output was not valid JSON.",
            code="NUEXTRACT_INVALID_JSON",
        ) from first_error
    raise NuExtractError(
        "model output was not valid JSON.",
        code="NUEXTRACT_INVALID_JSON",
    )


def _extract_response_text(response: Any) -> str:
    """Read generated text from an Ollama response in any supported shape."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        value = response.get("response", "")
        return value if isinstance(value, str) else ""
    value = getattr(response, "response", None)
    if isinstance(value, str):
        return value
    try:
        value = response["response"]  # mapping-like SDK objects
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


# ------------------------------------------------------------------ transport


def _is_default_endpoint(base_url: str) -> bool:
    return (base_url or "").strip().rstrip("/").lower() in _SDK_DEFAULT_HOSTS


def _raw_generate(model: str, prompt: str, base_url: str, timeout_seconds: float, options: Dict[str, Any]) -> Any:
    """Single Ollama call: explicit host-bound client for custom endpoints."""
    if _is_default_endpoint(base_url):
        # Shared module client already targets the loopback default.
        return ollama.generate(
            model=model,
            prompt=prompt,
            options=options,
        )
    try:
        client = ollama.Client(host=base_url, timeout=timeout_seconds)
    except TypeError:
        client = ollama.Client(host=base_url)
    return client.generate(
        model=model,
        prompt=prompt,
        options=options,
    )


def _run_with_timeout(func: Callable[[], Any], timeout_seconds: float) -> Any:
    """Run ``func`` with a hard timeout (daemon worker never blocks exit)."""
    box: queue.Queue = queue.Queue(maxsize=1)

    def _target() -> None:
        try:
            box.put((True, func()))
        except BaseException as exc:  # noqa: BLE001 — re-raised to caller
            box.put((False, exc))

    worker = threading.Thread(target=_target, name="nuextract-ollama", daemon=True)
    worker.start()
    try:
        ok, payload = box.get(timeout=max(0.001, float(timeout_seconds)))
    except queue.Empty:
        raise NuExtractError(
            "request timed out.",
            code="NUEXTRACT_TIMEOUT",
        ) from None
    if not ok:
        raise payload
    return payload


def _looks_like_timeout(exc: BaseException) -> bool:
    """Whether ``exc`` represents a timed-out request (SDK or socket level)."""
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc).lower()
    return "timeout" in message or "timed out" in message


def _is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` from the Ollama transport is worth one more attempt."""
    if isinstance(exc, NuExtractError):
        return exc.code in ("NUEXTRACT_TIMEOUT",)
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (408, 429, 500, 502, 503, 504):
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "timeout",
            "timed out",
            "connection",
            "connect",
            "network",
            "unreachable",
            "refused",
            "reset by peer",
            "temporarily unavailable",
            " 502",
            " 503",
            " 504",
            " 429",
        )
    )


# ------------------------------------------------------------------ parsing


def parse_order_with_nuextract(raw_text: str) -> Dict[str, Any]:
    """Extract a structured order dict from ``raw_text`` via NuExtract/Ollama.

    Raises :class:`NuExtractError` on empty input, transport failure, timeout
    or unusable model output. Never fabricates order data on failure.
    """
    if not isinstance(raw_text, str):
        raise NuExtractError(
            "input must be text.",
            code="NUEXTRACT_EMPTY_INPUT",
        )
    text = raw_text.strip()
    if not text:
        raise NuExtractError(
            "input text is empty.",
            code="NUEXTRACT_EMPTY_INPUT",
        )

    settings = get_settings()
    model = settings.nuextract_model
    base_url = (settings.ollama_base_url or "").strip() or "http://127.0.0.1:11434"
    timeout_seconds = float(getattr(settings, "nuextract_timeout_seconds", 120.0))
    max_attempts = max(1, int(getattr(settings, "nuextract_max_attempts", 3)))
    backoff_seconds = float(getattr(settings, "nuextract_retry_backoff_seconds", 1.0))
    max_input_chars = max(256, int(getattr(settings, "nuextract_max_input_chars", 8000)))
    options = build_nuextract_options(settings)

    prompt = build_nuextract_prompt(text[:max_input_chars])

    breaker = get_dependency_breaker("nuextract", settings)
    if breaker is not None and not breaker.allow():
        logger.warning("nuextract.circuit_open_fail_fast", model=model)
        raise NuExtractError(
            "NuExtract circuit open; request rejected without network attempt.",
            code="NUEXTRACT_UNAVAILABLE",
        ) from CircuitOpenError("NuExtract circuit open")

    last_exc: NuExtractError | None = None
    for attempt in range(1, max_attempts + 1):
        metrics.incr("nuextract_requests_total", model=model)
        logger.info(
            "nuextract.request",
            model=model,
            endpoint=base_url,
            attempt=attempt,
            max_attempts=max_attempts,
            input_chars=len(text),
        )
        try:
            response = _run_with_timeout(
                lambda: _raw_generate(
                    model=model,
                    prompt=prompt,
                    base_url=base_url,
                    timeout_seconds=timeout_seconds,
                    options=options,
                ),
                timeout_seconds,
            )
        except NuExtractError as exc:
            last_exc = exc
            if exc.code == "NUEXTRACT_TIMEOUT":
                metrics.incr("nuextract_timeout_total", model=model)
            if _is_transient(exc) and attempt < max_attempts:
                metrics.incr("nuextract_retries_total", model=model, outcome="transient")
                metrics.incr("ai_api_retries_total", outcome="transient")
                logger.warning(
                    "nuextract.transient_failure",
                    model=model,
                    attempt=attempt,
                    error=str(exc),
                )
                _sleep(backoff_seconds * (attempt - 1))
                continue
            metrics.incr("nuextract_failure_total", model=model)
            if breaker is not None:
                breaker.record_failure()
            logger.warning("nuextract.failed", model=model, code=exc.code)
            raise
        except Exception as exc:  # transport/SDK errors -> classify
            transient = _is_transient(exc)
            # SDK/socket timeouts surface as TimeoutError (or timeout text);
            # classify them as NUEXTRACT_TIMEOUT, other transport failures as
            # NUEXTRACT_UNAVAILABLE. Both are retried while attempts remain.
            code = "NUEXTRACT_TIMEOUT" if _looks_like_timeout(exc) else "NUEXTRACT_UNAVAILABLE"
            if code == "NUEXTRACT_TIMEOUT":
                metrics.incr("nuextract_timeout_total", model=model)
            last_exc = NuExtractError(
                f"Ollama request failed: {exc}",
                code=code,
            )
            if transient and attempt < max_attempts:
                metrics.incr("nuextract_retries_total", model=model, outcome="transient")
                metrics.incr("ai_api_retries_total", outcome="transient")
                logger.warning(
                    "nuextract.transient_failure",
                    model=model,
                    attempt=attempt,
                    error=str(exc),
                )
                _sleep(backoff_seconds * (attempt - 1))
                continue
            metrics.incr("nuextract_failure_total", model=model)
            if breaker is not None:
                breaker.record_failure()
            logger.warning(
                "nuextract.failed",
                model=model,
                code=last_exc.code,
            )
            raise last_exc from exc

        try:
            parsed = _clean_json(_extract_response_text(response))
        except NuExtractError as exc:
            last_exc = exc
            if exc.code == "NUEXTRACT_INVALID_JSON":
                metrics.incr("nuextract_invalid_json_total", model=model)
            # Malformed output occasionally resolves on a fresh attempt, but
            # only within the bounded attempt budget — never endlessly.
            metrics.incr("nuextract_retries_total", model=model, outcome="invalid_json")
            metrics.incr("ai_api_retries_total", outcome="invalid_json")
            logger.warning("nuextract.invalid_json_retry", model=model, attempt=attempt)
            if attempt < max_attempts:
                _sleep(backoff_seconds * (attempt - 1))
                continue
            metrics.incr("nuextract_failure_total", model=model)
            if breaker is not None:
                breaker.record_failure()
            raise

        if breaker is not None:
            breaker.record_success()
        metrics.incr("nuextract_success_total", model=model)
        logger.info(
            "nuextract.success",
            model=model,
            items=len(parsed.get("items", []) or []),
        )
        return parsed

    assert last_exc is not None
    metrics.incr("nuextract_failure_total", model=model)
    if breaker is not None:
        breaker.record_failure()
    raise last_exc


class TextParser:
    """Semantic text extraction via the locally hosted NuExtract model (Ollama).

    OCR is handled separately by Google Vision; this parser receives
    already-extracted text and returns structured order JSON for
    ``OrderNormalizer``. The public ``parse()`` interface is unchanged, so
    existing processors (text/image/PDF/Excel-fallback) keep working.
    """

    def __init__(self, client: Callable[[str, str], str] | None = None):
        settings = get_settings()
        # Optional test seam: ``client(model, prompt) -> raw JSON string``.
        # Production always goes through Ollama (never an external LLM).
        self._client = client
        self.model = settings.nuextract_model
        self.max_attempts = max(1, int(getattr(settings, "nuextract_max_attempts", 3)))
        self.backoff_seconds = float(getattr(settings, "nuextract_retry_backoff_seconds", 1.0))
        self.max_input_chars = max(256, int(getattr(settings, "nuextract_max_input_chars", 8000)))
        self.options = build_nuextract_options(settings)

    def _transport(self, model: str, prompt: str) -> str:
        if self._client is not None:
            return self._client(model, prompt)
        settings = get_settings()
        base_url = (settings.ollama_base_url or "").strip() or "http://127.0.0.1:11434"
        timeout_seconds = float(getattr(settings, "nuextract_timeout_seconds", 120.0))
        response = _run_with_timeout(
            lambda: _raw_generate(
                model=model,
                prompt=prompt,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
                options=build_nuextract_options(settings),
            ),
            timeout_seconds,
        )
        return _extract_response_text(response)

    def parse(self, content: str) -> Dict[str, Any]:
        if self._client is None:
            return parse_order_with_nuextract(content)
        # Injected-transport path (tests/embeddings): same prompt, JSON
        # recovery and bounded retries, without touching Ollama.
        if not isinstance(content, str) or not content.strip():
            raise NuExtractError(
                "input text is empty.",
                code="NUEXTRACT_EMPTY_INPUT",
            )
        prompt = build_nuextract_prompt(content.strip()[: self.max_input_chars])
        last_exc: NuExtractError | None = None
        for attempt in range(1, self.max_attempts + 1):
            raw = self._transport(self.model, prompt)
            try:
                return _clean_json(raw)
            except NuExtractError as exc:
                last_exc = exc
                metrics.incr("ai_api_retries_total", outcome="invalid_json")
                logger.warning("nuextract.invalid_json_retry", model=self.model, attempt=attempt)
                if attempt < self.max_attempts:
                    _sleep(self.backoff_seconds * (attempt - 1))
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _clean_json(raw: str) -> Dict[str, Any]:
        return _clean_json(raw)
