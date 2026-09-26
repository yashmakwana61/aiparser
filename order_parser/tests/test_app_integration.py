"""Phase 17: full-application integration smoke tests.

Boots the REAL FastAPI app (routers + CORS + metrics middleware + lifespan)
with a sanitized environment: every external channel unconfigured so the
lifespan starts clean without touching Telegram, IMAP, Odoo or Puter.
Locks together the Phase 7/8/16 wiring that no unit test exercises.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from order_parser.api.auth import apply_cors
from order_parser.config import Settings, get_settings
from order_parser.main import app

SMOKE_TOKEN = "smoke-token-123"

# NOTE: empty-string overrides, NOT delenv — Settings() also reads the
# developer's real `.env` file, and only actual environment variables take
# precedence over it (the Phase 9 lesson).
NETWORK_ENV_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_URL",
    "EMAIL_IMAP_HOST",
    "EMAIL_USERNAME",
    "ODOO_DB",
    "ODOO_USER",
    "ODOO_PASSWORD",
    "PUTER_AUTH_TOKEN",
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for key in NETWORK_ENV_KEYS:
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("API_AUTH_TOKEN", SMOKE_TOKEN)
    monkeypatch.setenv("ENABLE_ORDER_SESSIONS", "false")
    monkeypatch.setenv("ENABLE_RETENTION_SWEEPER", "false")
    # Deterministic regardless of host disk usage; thresholds are unit-tested.
    monkeypatch.setenv("DISK_CHECK_ENABLED", "false")
    get_settings.cache_clear()
    try:
        with TestClient(app) as test_client:  # runs lifespan
            yield test_client
    finally:
        get_settings.cache_clear()


def auth_header() -> dict:
    return {"Authorization": f"Bearer {SMOKE_TOKEN}"}


# ------------------------------------------------------------------ basics


def test_health_is_open_and_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_describes_service(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "AI Order Parser"


def test_unknown_route_returns_404(client):
    assert client.get("/definitely-not-a-route").status_code == 404


# ------------------------------------------------------------------ ready


def test_ready_reports_sanitized_components(client):
    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    components = body["components"]
    assert components["pipeline"] == "ok"
    assert components["odoo"] == "unconfigured"
    assert components["telegram"] == "unconfigured"
    assert components["email_imap"] == "unconfigured"
    assert components["pending_store"] == "ok"
    assert components["storage"] == "disabled"


# ----------------------------------------------------------------- metrics


def test_metrics_endpoint_requires_token(client):
    assert client.get("/metrics").status_code == 401


def test_metrics_exposes_http_counters_after_traffic(client):
    client.get("/health")  # generate one instrumented request
    response = client.get("/metrics", headers=auth_header())

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "http_requests_total" in response.text


# ------------------------------------------------------------- correlation


def test_request_id_echoed_when_well_formed(client):
    response = client.get("/health", headers={"X-Request-ID": "abc-123-XYZ"})

    assert response.headers["X-Request-ID"] == "abc-123-XYZ"


def test_malformed_request_id_replaced_not_echoed(client):
    response = client.get("/health", headers={"X-Request-ID": "bad id with spaces!\x01"})

    echoed = response.headers.get("X-Request-ID", "")
    assert echoed and echoed != "bad id with spaces!\x01"


# -------------------------------------------------------------- CORS wiring


def test_cors_preflight_allowed_for_configured_origin_only():
    # Middleware is installed at import time on the shared app, so exercise
    # apply_cors directly on a fresh app with explicit settings.
    settings = Settings(api_auth_token=SMOKE_TOKEN, api_cors_origins="https://staff.example.com")
    fresh_app = FastAPI()

    @fresh_app.get("/ping")
    async def ping():
        return {"ok": True}

    apply_cors(fresh_app, settings)
    cors_client = TestClient(fresh_app)

    preflight = cors_client.options(
        "/ping",
        headers={
            "Origin": "https://staff.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.status_code in (200, 204)
    assert (
        preflight.headers.get("access-control-allow-origin") == "https://staff.example.com"
    )

    denied = cors_client.options(
        "/ping",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert denied.headers.get("access-control-allow-origin") is None


# ------------------------------------------------------- gated review queue


def test_orders_queue_gated_then_served_through_real_stack(client):
    denied = client.get("/orders")
    assert denied.status_code == 401

    allowed = client.get("/orders", headers=auth_header())
    assert allowed.status_code == 200
    body = allowed.json()
    assert set(body.keys()) >= {"orders", "total", "limit", "offset"}
    assert body["orders"] == []
    assert body["total"] == 0


def test_audit_verify_gated_and_clean_on_fresh_install(client):
    assert client.get("/audit/verify").status_code == 401

    response = client.get("/audit/verify", headers=auth_header())
    assert response.status_code == 200
    assert response.json()["integrity_ok"] is True
