"""Phase 10 hardening: Telegram delivery retries, callback robustness, store quarantine."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import telegram.error

from order_parser.channels import telegram_handler as tg
from order_parser.channels.telegram_handler import (
    TelegramHandler,
    retry_telegram_call,
    send_message_with_retry,
)
from order_parser.core.metrics import REGISTRY
from order_parser.sessions import store as store_module
from order_parser.sessions.store import SessionStore
from order_parser.tests.test_session_store import make_session
from order_parser.tests.test_telegram_sessions import (
    FakeMessage,
    FakeQuery,
    build_handler,
    make_update,
    make_update_callback,
)
from order_parser.tests.test_session_service import FakePipeline


# ------------------------------------------------------------------- helpers


async def fake_sleep(seconds: float) -> None:
    fake_sleep.calls.append(seconds)


fake_sleep.calls = []  # type: ignore[attr-defined]


@pytest.fixture()
def sleeps():
    fake_sleep.calls = []
    return fake_sleep.calls


@pytest.fixture()
def fast_retry(monkeypatch):
    """Route handler-internal retries through a no-op sleeper."""
    fake_sleep.calls = []
    real = tg.retry_telegram_call

    async def wrapper(factory, attempts=3, delay=1.0, **kwargs):
        return await real(factory, attempts=attempts, delay=delay, sleep=fake_sleep)

    monkeypatch.setattr(tg, "retry_telegram_call", wrapper)
    return fake_sleep.calls


@pytest.fixture(autouse=True)
def _isolate_metrics():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


class FlakyBot:
    def __init__(self, failures: int = 1):
        self.failures = failures
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise telegram.error.TimedOut()
        payload = {"chat_id": chat_id, "text": text}
        self.sent.append(payload)
        return payload


class FlakyReplyMessage(FakeMessage):
    def __init__(self, failures: int = 1, permanent_error: Exception | None = None):
        super().__init__("x")
        self.failures = failures
        self.permanent_error = permanent_error

    async def reply_text(self, text, reply_markup=None):
        if self.permanent_error is not None:
            raise self.permanent_error
        if self.failures > 0:
            self.failures -= 1
            raise telegram.error.TimedOut()
        await super().reply_text(text, reply_markup)


class DeafQuery(FakeQuery):
    async def answer(self, text=None, show_alert=False):
        raise telegram.error.BadRequest("Query is too old and response timeout expired or query id is invalid")


def capture_metric(name: str, **labels: str) -> float:
    return REGISTRY.counter_value(name, **labels)


# ------------------------------------------------------- retry_telegram_call


@pytest.mark.usefixtures("fast_retry")
def test_retry_succeeds_after_transient_failures():
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise telegram.error.TimedOut()
        return "ok"

    result = asyncio.run(retry_telegram_call(flaky, sleep=fake_sleep))
    assert result == "ok"
    assert attempts["n"] == 3
    assert capture_metric("telegram_api_retries_total", outcome="transient") == 2


@pytest.mark.usefixtures("fast_retry")
def test_retryafter_waits_requested_time():
    state = {"n": 0}

    async def flood_limited():
        state["n"] += 1
        if state["n"] == 1:
            raise telegram.error.RetryAfter(retry_after=7)
        return "sent"

    waits = []

    async def recorder(seconds):
        waits.append(seconds)

    result = asyncio.run(retry_telegram_call(flood_limited, sleep=recorder))
    assert result == "sent"
    assert waits and waits[0] >= 7
    assert capture_metric("telegram_api_retries_total", outcome="flood_wait") == 1


@pytest.mark.usefixtures("fast_retry")
def test_non_transient_error_propagates_immediately():
    calls = {"n": 0}

    async def broken():
        calls["n"] += 1
        raise telegram.error.BadRequest("chat not found")

    with pytest.raises(telegram.error.BadRequest):
        asyncio.run(retry_telegram_call(broken, attempts=3, sleep=fake_sleep))
    assert calls["n"] == 1
    assert capture_metric("telegram_api_retries_total") == 0


def test_send_message_with_retry_delivers_after_transient(fast_retry):
    bot = FlakyBot(failures=2)
    sent = asyncio.run(send_message_with_retry(bot, chat_id=42, text="hello"))
    assert sent is not None
    assert bot.sent == [{"chat_id": 42, "text": "hello"}]
    assert capture_metric("telegram_api_retries_total", outcome="transient") == 2


# ------------------------------------------------------------- _reply wrapper


def test_reply_survives_transient_failures(tmp_path, fast_retry):
    handler, _, _ = build_handler(tmp_path, FakePipeline())
    message = FlakyReplyMessage(failures=1)

    asyncio.run(handler._reply(message, "delivered"))

    assert message.replies == ["delivered"]
    assert capture_metric("telegram_api_retries_total", outcome="transient") == 1


def test_reply_never_raises_on_permanent_failure(tmp_path, fast_retry):
    handler, _, _ = build_handler(tmp_path, FakePipeline())
    message = FlakyReplyMessage(permanent_error=telegram.error.BadRequest("chat not found"))

    asyncio.run(handler._reply(message, "ignored"))

    assert message.replies == []


# ------------------------------------------------------- callback robustness


def test_unknown_callback_data_answered_without_action(tmp_path, fast_retry):
    handler, _, _ = build_handler(tmp_path, FakePipeline())
    query = FakeQuery("sess:bogus-action")

    asyncio.run(handler.handle_callback(make_update_callback(query), None))

    assert query.answered == [(None, False)]


def test_callback_without_message_surface_is_answered_alert(tmp_path, fast_retry):
    handler, _, _ = build_handler(tmp_path, FakePipeline())
    query = FakeQuery("sess:finish")
    query.message = None
    update = make_update_callback(query)
    update.effective_message = None

    asyncio.run(handler.handle_callback(update, None))

    assert query.answered[-1] == ("Original message unavailable.", True)


def test_expired_query_answer_failure_does_not_crash(tmp_path, fast_retry):
    pipeline = FakePipeline()
    pipeline.result = {"status": "pending", "order_id": "ord5", "confidence": 80}
    handler, manager, _ = build_handler(tmp_path, pipeline)
    asyncio.run(handler.handle_update(make_update(FakeMessage("/neworder")), None))
    item = FakeMessage("milk 5")
    asyncio.run(handler.handle_update(make_update(item), None))
    query = DeafQuery("sess:finish")

    asyncio.run(handler.handle_callback(make_update_callback(query), None))  # must not raise

    stored = manager.store.list_all()
    assert stored[0].status.value == "WAITING_CONFIRMATION"


def test_double_tap_finish_is_graceful(tmp_path, fast_retry):
    pipeline = FakePipeline()
    pipeline.result = {"status": "pending", "order_id": "ord7", "confidence": 88}
    handler, manager, _ = build_handler(tmp_path, pipeline)
    asyncio.run(handler.handle_update(make_update(FakeMessage("/neworder")), None))
    asyncio.run(handler.handle_update(make_update(FakeMessage("milk 5")), None))

    first = FakeQuery("sess:finish")
    asyncio.run(handler.handle_callback(make_update_callback(first), None))
    second = FakeQuery("sess:finish")
    asyncio.run(handler.handle_callback(make_update_callback(second), None))

    assert any(
        "No active order session" in r for r in second.message.replies
    ), second.message.replies
    stored = manager.store.list_all()
    assert stored[0].status.value == "WAITING_CONFIRMATION"


# ------------------------------------------------------ session store quarantine


def write_raw(store: SessionStore, name: str, payload: str) -> Any:
    path = store.directory / name
    path.write_text(payload, encoding="utf-8")
    return path


@pytest.mark.usefixtures("fast_retry")
def test_corrupt_json_file_quarantined_on_list_all(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.save(make_session("ses_good01"))
    corrupt = write_raw(store, "ses_bad001.json", "{not valid json")

    sessions = store.list_all()

    assert [s.session_id for s in sessions] == ["ses_good01"]
    assert not corrupt.exists()
    quarantined = list(store.directory.glob("ses_bad001.json.corrupt-*"))
    assert len(quarantined) == 1
    assert REGISTRY.counter_value("session_files_corrupt_total") >= 1


def test_schema_invalid_file_quarantined(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    bad = write_raw(store, "ses_wrong1.json", json.dumps({"unexpected": "shape"}))

    assert store.list_all() == []
    assert not bad.exists()
    assert list(store.directory.glob("ses_wrong1.json.corrupt-*"))


def test_get_returns_none_and_quarantines_corrupt_file(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    write_raw(store, "ses_dead01.json", "garbage bytes \x00\xff")

    assert store.get("ses_dead01") is None
    assert not (store.directory / "ses_dead01.json").exists()


def test_unreadable_file_skipped_but_not_quarantined(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    store.save(make_session("ses_ok0001"))
    unreadable = write_raw(
        store, "ses_err01.json", json.dumps({"session_id": "ses_err01"})
    )
    real_loads = store_module.json.loads

    def boom(payload, *args, **kwargs):
        if "ses_err01" in payload:
            raise OSError("permission denied")
        return real_loads(payload)

    monkeypatch.setattr(store_module.json, "loads", boom)
    sessions = store.list_all()
    monkeypatch.undo()

    assert [s.session_id for s in sessions] == ["ses_ok0001"]
    assert unreadable.exists(), "OSError files must be left in place for ops"


def test_save_is_durable_and_reloadable(tmp_path):
    store = SessionStore(tmp_path / "sessions")

    store.save(make_session("ses_fsync1"))

    reloaded = store.get("ses_fsync1")
    assert reloaded is not None
    assert reloaded.session_id == "ses_fsync1"
    assert not list((tmp_path / "sessions").glob(".*tmp"))
