"""Phase 9: email ingestion hardening - dedup, guards, backoff."""
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from order_parser.channels.email_handler import (
    EmailHandler,
    backoff_delay,
    content_hash,
    normalize_message_id,
)
from order_parser.config import Settings
from order_parser.core.email_state_store import EmailStateStore


# ------------------------------------------------------------------- store


def _store(tmp_path, name="email_state.sqlite3"):
    return EmailStateStore(tmp_path / name)


def test_seen_roundtrip(tmp_path):
    store = _store(tmp_path)
    assert store.already_seen("m1") is False
    store.mark_seen("m1", source="imap")
    assert store.already_seen("m1") is True


def test_seen_window_expiry(tmp_path):
    store = _store(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    store.mark_seen("old", seen_at=old)
    assert store.already_seen("old", window_days=7) is True
    assert store.already_seen("old", window_days=1) is False


def test_state_persists_across_instances(tmp_path):
    _store(tmp_path).mark_seen("m2")
    assert _store(tmp_path).count() == 1


def test_corrupt_store_degrades_gracefully(tmp_path):
    path = tmp_path / "broken.sqlite3"
    path.write_bytes(b"not a database")
    store = EmailStateStore(path)
    assert store.already_seen("k") is False
    store.mark_seen("k")  # must not raise


def test_purge_removes_old_entries(tmp_path):
    store = _store(tmp_path)
    fresh = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    store.mark_seen("a", seen_at=old)
    store.mark_seen("b", seen_at=fresh)
    assert store.purge(older_than_days=30) == 1
    assert store.count() == 1


# ------------------------------------------------------------- key helpers


def test_normalize_message_id_strips_and_casefolds():
    assert normalize_message_id("<ABC@Mail.com>") == "abc@mail.com"
    assert normalize_message_id("  ") == ""


def test_content_hash_is_stable():
    assert content_hash(b"x" * 10) == content_hash(b"x" * 10)
    assert content_hash(b"a") != content_hash(b"b")


def test_backoff_growth_and_cap():
    assert backoff_delay(0, 60, 600) == 60
    assert backoff_delay(1, 60, 600) == 120
    assert backoff_delay(3, 60, 600) == 480
    assert backoff_delay(4, 60, 600) == 600  # capped
    assert backoff_delay(50, 60, 600) == 600  # no overflow


# ----------------------------------------------------------------- handler


class RecordingPipeline:
    def __init__(self):
        self.calls = []

    def process(self, source, input_type, parsed, raw=None):
        self.calls.append((source, input_type))
        return {"status": "success", "source": source}


def _handler(tmp_path, pipeline=None, **overrides):
    defaults = dict(
        email_max_attachment_mb=1,
        email_seen_window_days=7,
    )
    defaults.update(overrides)
    settings = Settings(
        email_state_db_path=str(tmp_path / "state.sqlite3"),
        log_dir=str(tmp_path),
    )
    # Stub out the real AI-backed processors by default so body-only email
    # tests never touch the network (PUTER_AUTH_TOKEN). Attachment routing is
    # still covered by test_supported_attachments_route_to_pipeline, which
    # injects its own explicit stubs.
    stubs = SimpleNamespace(
        text=SimpleNamespace(process=lambda body: object()),
        pdf=SimpleNamespace(process=lambda data, filename="": object()),
        excel=SimpleNamespace(process=lambda data, filename="": object()),
        image=SimpleNamespace(process=lambda data, filename="": object()),
    )
    handler = EmailHandler(pipeline or RecordingPipeline(), state_store=EmailStateStore(settings.email_state_db_path), processors=stubs)
    # The constructor reads global cached settings; tests pin guards explicitly.
    handler.max_attachment_bytes = int(defaults["email_max_attachment_mb"]) * 1024 * 1024
    handler.seen_window_days = int(defaults["email_seen_window_days"])
    return handler


def _raw_email(body="Order: bread x20", message_id=None, attachments=()):
    msg = EmailMessage()
    msg["From"] = "buyer@example.com"
    msg["Subject"] = "PO"
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(body)
    for filename, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return bytes(msg)


def test_text_email_processed_once_then_duplicate(tmp_path):
    pipeline = RecordingPipeline()
    handler = _handler(tmp_path, pipeline)
    raw = _raw_email(message_id="<po-1@example.com>")
    first = handler.process_raw_email(raw)
    assert first and first[0]["status"] == "success"
    second = handler.process_raw_email(raw)
    assert second[0]["status"] == "duplicate"
    assert len(pipeline.calls) == 1


def test_duplicate_detection_without_message_id_uses_content_hash(tmp_path):
    pipeline = RecordingPipeline()
    handler = _handler(tmp_path, pipeline)
    raw = _raw_email(body="no message id here")
    assert handler.process_raw_email(raw)[0]["status"] == "success"
    assert handler.process_raw_email(raw)[0]["status"] == "duplicate"


def test_different_emails_are_both_processed(tmp_path):
    pipeline = RecordingPipeline()
    handler = _handler(tmp_path, pipeline)
    handler.process_raw_email(_raw_email(body="one", message_id="<a@x>"))
    results = handler.process_raw_email(_raw_email(body="two", message_id="<b@x>"))
    assert results[0]["status"] == "success"
    assert len(pipeline.calls) == 2


def test_window_expiry_allows_reingestion(tmp_path):
    handler = _handler(tmp_path, email_seen_window_days=0)
    raw = _raw_email(message_id="<short-window@x>")
    assert handler.process_raw_email(raw)[0]["status"] == "success"

    from order_parser.core import email_state_store as ess

    stale = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(handler.state_store.path)
    conn.execute("UPDATE seen_messages SET seen_at = ?", (stale,))
    conn.commit()
    conn.close()
    # window of 0 days means only entries from "now" count; aged entry re-ingests
    again = handler.process_raw_email(raw)
    assert again[0]["status"] == "success"


def test_oversize_attachment_skipped_with_reason(tmp_path):
    pipeline = RecordingPipeline()
    handler = _handler(tmp_path, pipeline)
    big = b"x" * (1024 * 1024 + 1)  # > 1 MB limit
    raw = _raw_email(
        body="see attachment",
        attachments=[("huge.pdf", big, "application", "pdf")],
    )
    results = handler.process_raw_email(raw)
    assert results[0]["reason"] == "attachment_too_large"
    assert ("email", "pdf") not in pipeline.calls


def test_unsupported_attachment_type_skipped(tmp_path):
    pipeline = RecordingPipeline()
    handler = _handler(tmp_path, pipeline)
    raw = _raw_email(
        body="malware",
        attachments=[("setup.exe", b"MZ...", "application", "octet-stream")],
    )
    results = handler.process_raw_email(raw)
    assert results[0]["reason"] == "unsupported_attachment_type"
    assert not pipeline.calls


def test_supported_attachments_route_to_pipeline(tmp_path):
    pipeline = RecordingPipeline()
    stubs = SimpleNamespace(
        text=SimpleNamespace(process=lambda body: object()),
        pdf=SimpleNamespace(process=lambda data, filename="": object()),
        excel=SimpleNamespace(process=lambda data, filename="": object()),
        image=SimpleNamespace(process=lambda data, filename="": object()),
    )
    handler = EmailHandler(
        pipeline,
        state_store=EmailStateStore(str(tmp_path / "state.sqlite3")),
        processors=stubs,
    )
    raw = _raw_email(
        body="two files",
        attachments=[
            ("order.pdf", b"%PDF-1.4 fake", "application", "pdf"),
            ("sheet.xlsx", b"PK\x03\x04 fake", "application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ("scan.png", b"\x89PNG fake", "image", "png"),
        ],
    )
    results = [r for r in handler.process_raw_email(raw)]
    routed = {input_type for _, input_type in pipeline.calls}
    assert routed == {"pdf", "excel", "image"}
    assert all(r["status"] == "success" for r in results)


def test_poll_disabled_without_config(tmp_path, monkeypatch):
    handler = _handler(tmp_path)
    import order_parser.channels.email_handler as eh_module

    # Explicitly empty email config: never read .env / touch the network.
    monkeypatch.setattr(eh_module, "get_settings", lambda: Settings(email_imap_host="", email_username=""))
    assert handler.poll() == 0
