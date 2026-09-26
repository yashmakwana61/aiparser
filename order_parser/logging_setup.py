from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from order_parser.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    if not root.handlers:
        logging.basicConfig()

    if settings.log_to_file:
        log_dir = Path(settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        already = any(isinstance(h, RotatingFileHandler) for h in root.handlers)
        if not already:
            handler = RotatingFileHandler(
                log_dir / "app.log",
                maxBytes=max(1, int(settings.log_file_max_mb)) * 1024 * 1024,
                backupCount=max(0, int(settings.log_file_backup_count)),
                encoding="utf-8",
            )
            root.addHandler(handler)


logger = structlog.get_logger()
