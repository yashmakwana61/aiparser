"""Observability endpoints and HTTP instrumentation (Phase 8).

- GET /ready   : readiness probe. 200 only when critical local components
                 are healthy; Odoo/Telegram/email being unconfigured is
                 reported but does not fail readiness (orders queue safely).
- GET /metrics : Prometheus text exposition of the in-process registry.
                 Protected by the shared API token when one is configured.
- install_http_metrics: request counter + duration observation + X-Request-ID
                 correlation bound into structlog contextvars.
"""
from __future__ import annotations

import shutil
import time
import uuid

import structlog
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from order_parser.api.auth import require_api_key
from order_parser.config import get_settings
from order_parser.core.audit import verify_audit_directory
from order_parser.core.metrics import REGISTRY

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["monitoring"])


def _component_store(store) -> str:
    if store is None:
        return "disabled"
    try:
        count = store.count()
        return "ok" if isinstance(count, int) else "error"
    except Exception:
        logger.exception("monitoring.store_check_failed")
        return "error"


def _component_pending(store) -> str:
    if store is None:
        return "error"
    try:
        records = store.list()
        return "ok" if isinstance(records, list) else "error"
    except Exception:
        logger.exception("monitoring.pending_check_failed")
        return "error"


def _component_storage(settings) -> str:
    if not settings.disk_check_enabled:
        return "disabled"
    try:
        free_bytes = shutil.disk_usage(settings.log_dir).free
    except OSError:
        logger.exception("monitoring.disk_check_failed")
        return "error"
    free_gb = free_bytes / (1024**3)
    if free_gb < settings.disk_min_free_error_gb:
        return "error"
    if free_gb < settings.disk_min_free_warning_gb:
        return "warning"
    return "ok"


@router.get("/ready")
async def ready(request: Request) -> Response:
    components: dict[str, str] = {}
    pipeline = getattr(request.app.state, "pipeline", None)
    components["pipeline"] = "ok" if pipeline is not None else "error"
    if pipeline is not None:
        odoo = getattr(pipeline, "odoo", None)
        components["odoo"] = "configured" if getattr(odoo, "enabled", False) else "unconfigured"
        components["resolver"] = (
            "loaded" if getattr(pipeline, "resolver", None) is not None else "legacy_mode"
        )
        components["idempotency_store"] = _component_store(getattr(pipeline, "idempotency", None))
        components["pending_store"] = _component_pending(getattr(pipeline, "pending_store", None))

    settings = get_settings()
    components["telegram"] = "configured" if settings.telegram_bot_token else "unconfigured"
    components["email_imap"] = "configured" if settings.email_imap_host else "unconfigured"
    components["storage"] = _component_storage(settings)
    # Job queue health
    job_queue = getattr(request.app.state, "job_queue", None)
    if job_queue is not None:
        try:
            stats = job_queue.stats()
            if stats.get("running"):
                components["job_queue"] = "ok"
            else:
                components["job_queue"] = "idle"
            # surface depth as informational, not readiness failure
            components["job_queue_depth"] = str(stats.get("depth", 0))
        except Exception:
            components["job_queue"] = "error"
    else:
        components["job_queue"] = "disabled"
    job_store = getattr(request.app.state, "job_store", None)
    components["job_store"] = _component_store(job_store)

    hard_errors = [name for name, status in components.items() if status == "error"]
    ready_ok = not hard_errors
    payload = {"ready": ready_ok, "components": components}
    return JSONResponse(status_code=200 if ready_ok else 503, content=payload)


@router.get("/metrics", dependencies=[Depends(require_api_key)])
async def prometheus_metrics() -> Response:
    return Response(
        content=REGISTRY.render_prometheus(),
        media_type="text/plain; version=1.0.0; charset=utf-8",
    )


@router.get("/audit/verify", dependencies=[Depends(require_api_key)])
async def audit_verify() -> Response:
    """Tamper-evidence check across the whole daily audit archive.

    Mirrors /ready semantics: 200 when every daily chain is intact, 503 with
    the per-file break list otherwise.
    """
    report = verify_audit_directory()
    return JSONResponse(status_code=200 if report["integrity_ok"] else 503, content=report)


def _sanitize_request_id(raw: str) -> str | None:
    raw = raw.strip()[:64]
    if raw and all(33 <= ord(ch) <= 126 for ch in raw):
        return raw
    return None


def install_http_metrics(app) -> None:
    """Attach the request-metrics/correlation middleware to an app."""

    @app.middleware("http")
    async def http_observability(request: Request, call_next):
        request_id = _sanitize_request_id(request.headers.get("x-request-id", "")) or uuid.uuid4().hex[:16]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.monotonic()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            REGISTRY.incr("http_requests_total", method=request.method, path=_route_path(request), status="500")
            REGISTRY.observe("http_request_duration_seconds", time.monotonic() - started)
            structlog.contextvars.unbind_contextvars("request_id")
            raise
        duration = time.monotonic() - started
        path = _route_path(request)
        REGISTRY.incr("http_requests_total", method=request.method, path=path, status=str(status))
        REGISTRY.observe("http_request_duration_seconds", duration)
        try:
            response.headers["X-Request-ID"] = request_id
        except Exception:  # pragma: no cover - header injection must not break responses
            pass
        structlog.contextvars.unbind_contextvars("request_id")
        return response


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path) or request.url.path
