from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from order_parser.api.aliases import router as aliases_router
from order_parser.api.auth import apply_cors
from order_parser.api.email_webhook import router as email_router
from order_parser.api.jobs import parse_router, router as jobs_router
from order_parser.api.monitoring import install_http_metrics, router as monitoring_router
from order_parser.api.orders import router as orders_router
from order_parser.api.telegram_webhook import router as telegram_router
from order_parser.core.job_queue import JobQueue
from order_parser.core.job_store import JobStore
from order_parser.channels.email_handler import EmailHandler, backoff_delay
from order_parser.channels.telegram_handler import TelegramHandler, send_message_with_retry
from order_parser.config import get_settings
from order_parser.core.retention import run_retention_sweep
from order_parser.integrations.odoo_client import OdooClient
from order_parser.logging_setup import configure_logging
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.catalog import CatalogProvider
from order_parser.resolution.order_resolver import OrderResolver
from order_parser.services.pipeline import OrderPipeline
from order_parser.services.session_service import SessionService
from order_parser.sessions.manager import SessionManager
from order_parser.sessions.staff_registry import StaffRegistry
from order_parser.sessions.store import SessionStore
from order_parser.utils import ensure_directory

configure_logging()
logger = structlog.get_logger(__name__)


async def email_polling_loop(handler: EmailHandler, interval: int, max_backoff: int = 600) -> None:
    failures = 0
    while True:
        try:
            processed = await asyncio.to_thread(handler.poll)
            failures = 0
            if processed:
                logger.info("email.poll_processed", count=processed)
        except Exception as exc:
            failures += 1
            delay = backoff_delay(failures, interval, max_backoff)
            logger.warning(
                "email.poll_failed_backoff",
                failures=failures,
                delay_seconds=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
            continue
        await asyncio.sleep(interval)


async def session_expiry_loop(manager: SessionManager, bot, interval: int = 60) -> None:
    """Expire idle order sessions; staff are notified, no order is created."""
    while True:
        try:
            expired = await asyncio.to_thread(manager.expire_stale)
            for session in expired:
                logger.info("session.expired_sweep", session_id=session.session_id)
                if session.chat_id is not None and bot is not None:
                    try:
                        await send_message_with_retry(
                            bot,
                            session.chat_id,
                            "⌛ Your order session expired. Please start a new order with /neworder.",
                        )
                    except Exception:
                        logger.exception("session.expiry_notify_failed", session_id=session.session_id)
        except Exception:
            logger.exception("session.expiry_sweep_failed")
        await asyncio.sleep(interval)


async def retention_loop(
    session_store: SessionStore,
    idempotency_store,
    retention_days: int,
    interval_seconds: int,
    audit_retention_days: int = 0,
) -> None:
    """Periodically reclaim disk/DB space from terminal sessions and old history."""
    while True:
        try:
            await asyncio.to_thread(
                run_retention_sweep,
                session_store,
                idempotency_store,
                retention_days,
                None,
                audit_retention_days,
            )
        except Exception:
            logger.exception("retention.sweep_failed")
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ensure_directory(settings.log_dir)
    if not settings.api_auth_token:
        logger.warning("api.auth_disabled_set_api_auth_token_in_production")
    if not settings.api_rate_limit_per_minute:
        logger.warning("api.rate_limiting_disabled")

    odoo = OdooClient()
    alias_store = AliasStore()
    catalog = CatalogProvider(odoo)
    resolver = OrderResolver(odoo=odoo, catalog=catalog, alias_store=alias_store)

    pipeline = OrderPipeline(odoo, resolver=resolver)
    app.state.pipeline = pipeline
    app.state.odoo = odoo
    app.state.catalog = catalog
    app.state.alias_store = alias_store
    app.state.telegram_app = None

    job_store = JobStore()
    job_queue = JobQueue(job_store=job_store)
    await job_queue.start()
    app.state.job_store = job_store
    app.state.job_queue = job_queue

    staff_registry = StaffRegistry(settings.authorized_staff)
    session_store = SessionStore()
    session_manager = SessionManager(
        session_store, timeout_minutes=settings.order_session_timeout_minutes
    )
    # All processing goes through Job API as source of truth
    session_service = SessionService(pipeline, job_store=job_store)
    email_handler = EmailHandler(pipeline, job_store=job_store)
    app.state.email_handler = email_handler
    sessions_enabled = settings.enable_order_sessions and settings.telegram_bot_token
    if settings.enable_order_sessions and not settings.telegram_bot_token:
        logger.warning("sessions.telegram_not_configured")
    if staff_registry.enforced:
        logger.info("staff_registry.enforced", staff=staff_registry.staff_ids)
    else:
        logger.warning("staff_registry.not_configured_authorization_disabled")
    app.state.staff_registry = staff_registry
    app.state.session_store = session_store
    app.state.session_manager = session_manager

    if not pipeline.odoo.enabled:
        logger.warning("odoo.not_configured_orders_will_be_routed_to_review")

    telegram_app = None
    expiry_task = None
    if settings.telegram_bot_token:
        if sessions_enabled:
            telegram_handler = TelegramHandler(
                pipeline,
                session_manager=session_manager,
                staff_registry=staff_registry,
                session_service=session_service,
                job_store=job_store,
                job_queue=job_queue,
            )
        else:
            telegram_handler = TelegramHandler(pipeline, staff_registry=staff_registry, job_store=job_store, job_queue=job_queue)
        telegram_app = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .connect_timeout(30)
            .read_timeout(60)
            .write_timeout(30)
            .media_write_timeout(120)
            .build()
        )
        telegram_app.add_handler(MessageHandler(filters.ALL, telegram_handler.handle_update))
        # Callbacks (order-case buttons AND session buttons) must always be
        # served — gating them on sessions silently kills every inline button
        # in direct (session-less) mode.
        telegram_app.add_handler(CallbackQueryHandler(telegram_handler.handle_callback))
        await telegram_app.initialize()
        if settings.telegram_webhook_url:
            try:
                await telegram_app.bot.set_webhook(
                    settings.telegram_webhook_url,
                    secret_token=settings.telegram_webhook_secret,
                )
                logger.info("telegram.webhook_set", url=settings.telegram_webhook_url)
            except Exception as exc:
                # Never kill the whole service over a webhook registration
                # failure (bad URL, DNS not live yet, Telegram hiccup).
                # Fix TELEGRAM_WEBHOOK_URL and restart to register.
                logger.error("telegram.webhook_set_failed", url=settings.telegram_webhook_url, error=str(exc))
        else:
            await telegram_app.start()
            await telegram_app.updater.start_polling(drop_pending_updates=True)
            logger.info("telegram.polling_started")
        app.state.telegram_app = telegram_app
        if sessions_enabled:
            expiry_task = asyncio.create_task(session_expiry_loop(session_manager, telegram_app.bot))
            logger.info("sessions.enabled", timeout_minutes=settings.order_session_timeout_minutes)

    poller_task = None
    if settings.email_imap_host and settings.email_username:
        poller_task = asyncio.create_task(
            email_polling_loop(
                email_handler,
                settings.email_poll_interval,
                max_backoff=int(getattr(settings, "email_max_backoff_seconds", 600)),
            )
        )
        logger.info("email.poller_started", interval=settings.email_poll_interval)

    retention_task = None
    if settings.enable_retention_sweeper and (
        settings.session_retention_days > 0 or settings.audit_retention_days > 0
    ):
        retention_task = asyncio.create_task(
            retention_loop(
                session_store,
                getattr(pipeline, "idempotency", None),
                settings.session_retention_days,
                max(60, settings.retention_sweep_interval_seconds),
                audit_retention_days=settings.audit_retention_days,
            )
        )
        logger.info(
            "retention.sweeper_started",
            days=settings.session_retention_days,
            audit_days=settings.audit_retention_days,
        )

    yield

    # shutdown job queue
    try:
        await app.state.job_queue.stop()
    except Exception:
        logger.exception("job_queue.shutdown_failed")

    background_tasks = [t for t in (expiry_task, poller_task, retention_task) if t]
    for task in background_tasks:
        task.cancel()
    if background_tasks:
        # Let loops finish their current iteration instead of abandoning
        # them mid-write; cancelled tasks surface no exception here.
        done, pending = await asyncio.wait(background_tasks, timeout=5.0)
        for task in pending:
            logger.warning("background.task_shutdown_timeout", task=task.get_name())
    if telegram_app is not None:
        if telegram_app.updater and telegram_app.updater.running:
            await telegram_app.updater.stop()
        if telegram_app.running:
            await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(title="AI Order Parser", version="1.2.0", lifespan=lifespan)

apply_cors(app, get_settings())
install_http_metrics(app)

app.include_router(telegram_router)
app.include_router(email_router)
app.include_router(orders_router)
app.include_router(aliases_router)
app.include_router(monitoring_router)
app.include_router(jobs_router)
app.include_router(parse_router)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "AI Order Parser", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("order_parser.main:app", host="0.0.0.0", port=8000, reload=True)