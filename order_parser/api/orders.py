from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from order_parser.api.auth import security_dependencies
from order_parser.services.pipeline import OrderPipeline

router = APIRouter(prefix="/orders", tags=["orders"], dependencies=security_dependencies())


def _pipeline(request: Request) -> OrderPipeline:
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline is not initialized")
    return pipeline


def _pending_store(request: Request):
    store = getattr(_pipeline(request), "pending_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Pending store is not initialized")
    return store


@router.get("")
async def list_orders(
    request: Request,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Review queue, newest first. Bounded pages keep responses predictable."""
    store = _pending_store(request)
    matching = store.list(status=status)
    page = matching[offset : offset + limit]
    return {"orders": page, "total": len(matching), "limit": limit, "offset": offset}


@router.get("/{order_id}")
async def get_order(order_id: str, request: Request) -> dict:
    pipeline = _pipeline(request)
    record = _pending_store(request).get(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="Order not found")
    # Review opens re-check Odoo for products created after ingest so
    # candidate buttons are never stale. Best-effort: failures keep
    # the stored candidates.
    try:
        from order_parser.user_actions.corrections import CorrectionService

        job_id = str(record.get("job_id") or "")
        if job_id:
            CorrectionService(
                getattr(request.app.state, "job_store", None),
                _pending_store(request), pipeline).refresh_product_candidates(job_id)
            record = _pending_store(request).get(order_id) or record
    except Exception:
        pass
    return {"order": record}


@router.post("/{order_id}/confirm")
async def confirm_order(order_id: str, request: Request) -> dict:
    result = _pipeline(request).confirm_order(order_id, actor="api")
    if isinstance(result, dict) and result.get("status") == "success":
        from order_parser.user_actions.case import mark_job_completed

        mark_job_completed(getattr(request.app.state, "job_store", None),
                           order_id, result.get("sales_order"))
    return result


@router.post("/{order_id}/reject")
async def reject_order(order_id: str, request: Request) -> dict:
    return _pipeline(request).reject_order(order_id, actor="api")
