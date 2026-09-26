"""Shared API security primitives (Phase 7).

- :func:`require_api_key` - bearer/API-key gate for management and ingestion
  endpoints. Enforcement is off while ``API_AUTH_TOKEN`` is empty so existing
  deployments keep working until operators opt in.
- :class:`RateLimiter` - tiny in-memory sliding-window limiter for mutating
  endpoints (single-process deployments; matches the local-store design).
- :func:`apply_cors` - opt-in CORS lockdown driven by ``API_CORS_ORIGINS``.

The Telegram delivery webhook is deliberately excluded: it authenticates via
its own ``X-Telegram-Bot-Api-Secret-Token`` shared secret.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from order_parser.config import Settings, get_settings
from order_parser.core import metrics

logger = structlog.get_logger(__name__)


def _settings() -> Settings:
    return get_settings()


def _extract_presented_key(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-api-key", "").strip()


async def require_api_key(request: Request) -> None:
    """FastAPI dependency enforcing the shared API token when configured."""
    settings = _settings()
    expected = settings.api_auth_token.strip()
    if not expected:
        return  # enforcement disabled (development mode)

    presented = _extract_presented_key(request)
    if not presented:
        metrics.incr("auth_denied_total", outcome="missing")
        raise HTTPException(
            status_code=401,
            detail="Missing API credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        metrics.incr("auth_denied_total", outcome="invalid")
        raise HTTPException(
            status_code=401,
            detail="Invalid API credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


class RateLimiter:
    """Sliding-window per-client rate limiter (in-memory, thread-safe)."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, bucket: str, client: str) -> tuple[bool, int]:
        """Register a hit. Returns (allowed, retry_after_seconds)."""
        limit = int(getattr(_settings(), "api_rate_limit_per_minute", 0))
        if limit <= 0:
            return True, 0
        window = 60.0
        now = time.monotonic()
        key = (bucket, client)
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, int(window - (now - hits[0])) + 1)
                return False, retry_after
            hits.append(now)
            return True, 0


mutation_rate_limiter = RateLimiter()


async def mutation_rate_limit(request: Request) -> None:
    allowed, retry_after = mutation_rate_limiter.hit("mutations", _client_key(request))
    if not allowed:
        metrics.incr("rate_limited_total")
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded; slow down",
            headers={"Retry-After": str(retry_after)},
        )


def _client_key(request: Request) -> str:
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def apply_cors(app: FastAPI, settings: Settings | None = None) -> None:
    """Restrict browser origins when API_CORS_ORIGINS is configured."""
    settings = settings or _settings()
    raw = getattr(settings, "api_cors_origins", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "X-API-Key", "Content-Type", "X-Telegram-Bot-Api-Secret-Token"],
        allow_credentials=False,
        max_age=600,
    )
    logger.info("api.cors_configured", origins=origins)


def security_dependencies() -> list:
    """Standard dependency chain for protected routes."""
    return [Depends(require_api_key), Depends(mutation_rate_limit)]
