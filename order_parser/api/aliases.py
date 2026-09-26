from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from order_parser.api.auth import security_dependencies
from order_parser.resolution.alias_store import AliasConflictError

logger = structlog.get_logger(__name__)

# Protected by the shared API token + rate limiter (Phase 7); enforcement is
# inactive until API_AUTH_TOKEN is configured.
router = APIRouter(prefix="/aliases", tags=["aliases"], dependencies=security_dependencies())


class ProductAliasCreate(BaseModel):
    raw_alias: str = Field(min_length=1)
    target_product_id: int
    customer_id: int | None = None
    created_by: str = "staff"


class CustomerAliasCreate(BaseModel):
    raw_alias: str = Field(min_length=1)
    target_partner_id: int
    created_by: str = "staff"


def _state(request: Request):
    alias_store = getattr(request.app.state, "alias_store", None)
    if alias_store is None:
        raise HTTPException(status_code=503, detail="Alias store is not initialized")
    return alias_store


@router.get("/products")
async def list_product_aliases(request: Request, query: str | None = None, active_only: bool = True) -> dict:
    aliases = _state(request).list_aliases("product", query=query, active_only=active_only)
    return {"aliases": [a.model_dump() for a in aliases]}


@router.post("/products", status_code=201)
async def create_product_alias(payload: ProductAliasCreate, request: Request) -> dict:
    store = _state(request)
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is not None:
        try:
            if catalog.get(payload.target_product_id) is None:
                raise HTTPException(status_code=404, detail=f"Product {payload.target_product_id} not found in Odoo catalog")
        except HTTPException:
            raise
        except Exception:
            logger.exception("aliases.catalog_check_failed")
    try:
        record = store.create_product(
            payload.raw_alias,
            payload.target_product_id,
            customer_id=payload.customer_id,
            created_by=payload.created_by,
        )
    except AliasConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"alias": record.model_dump()}


@router.get("/customers")
async def list_customer_aliases(request: Request, query: str | None = None, active_only: bool = True) -> dict:
    aliases = _state(request).list_aliases("customer", query=query, active_only=active_only)
    return {"aliases": [a.model_dump() for a in aliases]}


@router.post("/customers", status_code=201)
async def create_customer_alias(payload: CustomerAliasCreate, request: Request) -> dict:
    store = _state(request)
    odoo = getattr(request.app.state, "odoo", None)
    if odoo is not None and getattr(odoo, "enabled", False):
        try:
            if odoo.get_partner(payload.target_partner_id) is None:
                raise HTTPException(status_code=404, detail=f"Partner {payload.target_partner_id} not found in Odoo")
        except HTTPException:
            raise
        except Exception:
            logger.exception("aliases.partner_check_failed")
    try:
        record = store.create_customer(
            payload.raw_alias,
            payload.target_partner_id,
            created_by=payload.created_by,
        )
    except AliasConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"alias": record.model_dump()}
