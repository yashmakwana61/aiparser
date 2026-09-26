from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from order_parser.api.auth import security_dependencies
from order_parser.config import get_settings

router = APIRouter(prefix="/email", tags=["email"], dependencies=security_dependencies())


class EmailWebhookPayload(BaseModel):
    raw_email: str


@router.post("/webhook")
async def email_webhook(payload: EmailWebhookPayload, request: Request) -> dict:
    settings = get_settings()
    max_bytes = max(1, int(getattr(settings, "email_max_raw_mb", 25))) * 1024 * 1024
    raw_size = len(payload.raw_email.encode("utf-8", errors="replace"))
    if raw_size > max_bytes:
        raise HTTPException(status_code=413, detail="Raw email exceeds the maximum allowed size")
    handler = getattr(request.app.state, "email_handler", None)
    if handler is None:
        raise HTTPException(status_code=503, detail="Email handler is not initialized")
    results = await asyncio.to_thread(handler.process_raw_email, payload.raw_email.encode("utf-8"))
    return {"status": "success", "results": results}


@router.post("/poll")
async def email_poll(request: Request) -> dict:
    handler = getattr(request.app.state, "email_handler", None)
    if handler is None:
        raise HTTPException(status_code=503, detail="Email handler is not initialized")
    processed = await asyncio.to_thread(handler.poll)
    return {"processed": processed}