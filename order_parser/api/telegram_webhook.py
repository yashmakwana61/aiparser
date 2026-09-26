from __future__ import annotations

import secrets

import structlog
from fastapi import APIRouter, HTTPException, Request
from telegram import Update

from order_parser.api.auth import security_dependencies
from order_parser.config import get_settings

logger = structlog.get_logger(__name__)

# NOTE: /telegram/webhook authenticates with Telegram's own shared secret and
# is intentionally NOT behind the API token; the admin setup route is.
router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post("/webhook")
async def telegram_webhook(request: Request) -> dict:
    settings = get_settings()
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not secrets.compare_digest((secret or "").encode("utf-8"), settings.telegram_webhook_secret.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Invalid webhook secret token")

    telegram_app = getattr(request.app.state, "telegram_app", None)
    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")

    payload = await request.json()
    try:
        update = Update.de_json(payload, telegram_app.bot)
    except Exception:
        logger.warning("telegram.invalid_update", payload=payload)
        return {"ok": False, "reason": "invalid_update"}
    await telegram_app.process_update(update)
    return {"ok": True}


@router.post("/webhook/setup", dependencies=security_dependencies())
async def telegram_setup_webhook(request: Request) -> dict:
    settings = get_settings()
    telegram_app = getattr(request.app.state, "telegram_app", None)
    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    if not settings.telegram_webhook_url:
        raise HTTPException(status_code=400, detail="TELEGRAM_WEBHOOK_URL is not set")

    await telegram_app.bot.set_webhook(
        settings.telegram_webhook_url,
        secret_token=settings.telegram_webhook_secret,
    )
    return {"ok": True, "url": settings.telegram_webhook_url}