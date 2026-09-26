import json
import logging
from logging.handlers import RotatingFileHandler

import pytest
import structlog

from order_parser.api.monitoring import _component_storage
from order_parser.config import Settings, get_settings
from order_parser.logging_setup import configure_logging


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


def _file_handlers(root):
    return [h for h in root.handlers if isinstance(h, RotatingFileHandler)]


def test_file_logging_disabled_by_default(tmp_path, monkeypatch, clean_root):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.delenv("LOG_TO_FILE", raising=False)
    get_settings.cache_clear()
    try:
        configure_logging()
        logging.getLogger("smoke.off").info("no file please")
        assert not (tmp_path / "app.log").exists()
        assert _file_handlers(clean_root) == []
    finally:
        get_settings.cache_clear()


def test_file_logging_writes_jsonl(tmp_path, monkeypatch, clean_root):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_TO_FILE", "true")
    get_settings.cache_clear()
    try:
        configure_logging()
        slogger = structlog.get_logger("smoke.on")
        slogger.info("hello_file", order_id="ORD-1")
        for handler in _file_handlers(clean_root):
            handler.flush()

        content = (tmp_path / "app.log").read_text(encoding="utf-8").strip().splitlines()
        assert any(json.loads(line).get("order_id") == "ORD-1" for line in content)
    finally:
        get_settings.cache_clear()


def test_file_logging_rolls_over(tmp_path, monkeypatch, clean_root):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_TO_FILE", "true")
    monkeypatch.setenv("LOG_FILE_MAX_MB", "1")
    monkeypatch.setenv("LOG_FILE_BACKUP_COUNT", "1")
    get_settings.cache_clear()
    try:
        configure_logging()
        slogger = structlog.get_logger("smoke.roll")
        for i in range(1500):
            slogger.info("filler", blob="y" * 1024)
        for handler in _file_handlers(clean_root):
            handler.flush()

        assert (tmp_path / "app.log").exists()
        assert (tmp_path / "app.log.1").exists()
    finally:
        get_settings.cache_clear()


def test_configure_logging_is_idempotent(tmp_path, monkeypatch, clean_root):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_TO_FILE", "true")
    get_settings.cache_clear()
    try:
        configure_logging()
        configure_logging()
        assert len(_file_handlers(clean_root)) == 1
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------ storage


def test_storage_component_thresholds(monkeypatch):
    import order_parser.api.monitoring as monitoring

    settings = Settings(disk_check_enabled=True)

    monkeypatch.setattr(
        monitoring.shutil, "disk_usage", lambda _: type("U", (), {"free": 100 * 1024**3})()
    )
    assert _component_storage(settings) == "ok"

    monkeypatch.setattr(
        monitoring.shutil, "disk_usage", lambda _: type("U", (), {"free": 2 * 1024**3})()
    )
    assert _component_storage(settings) == "warning"

    monkeypatch.setattr(
        monitoring.shutil, "disk_usage", lambda _: type("U", (), {"free": 0.5 * 1024**3})()
    )
    assert _component_storage(settings) == "error"

    assert _component_storage(Settings(disk_check_enabled=False)) == "disabled"


def test_storage_component_error_on_oserror(monkeypatch):
    import order_parser.api.monitoring as monitoring

    def boom(_):
        raise OSError("volume gone")

    monkeypatch.setattr(monitoring.shutil, "disk_usage", boom)
    assert _component_storage(Settings()) == "error"
