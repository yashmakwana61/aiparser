"""Phase 7: API hardening - shared token auth, rate limiting, CORS."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from order_parser.api import auth as auth_module
from order_parser.api import telegram_webhook as tg_module
from order_parser.api.aliases import router as aliases_router
from order_parser.api.email_webhook import router as email_router
from order_parser.api.orders import router as orders_router
from order_parser.api.telegram_webhook import router as telegram_router
from order_parser.config import Settings

TOKEN = "unit-test-token"


class FakePendingStore:
    def get(self, order_id):
        return {"order_id": order_id, "status": "pending"}

    def delete(self, order_id):
        pass

    def list(self, status=None, limit=None, offset=0):
        # Phase 16: queue listing moved to the store; keep the auth tests'
        # fake compatible with the real interface.
        return [{"order_id": "ord1", "status": "pending"}]


class FakePipeline:
    def __init__(self):
        self.pending_store = FakePendingStore()

    def list_orders(self, status=None):
        return []

    def confirm_order(self, order_id, actor="api"):
        return {"status": "success", "sales_order": "SO00001"}

    def reject_order(self, order_id, actor="api"):
        return {"status": "rejected"}


def make_settings(**overrides):
    defaults = dict(
        api_auth_token=TOKEN,
        api_rate_limit_per_minute=0,
        api_cors_origins="",
        telegram_webhook_secret="tgsecret",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def _reset_limiter():
    auth_module.mutation_rate_limiter._hits.clear()
    yield
    auth_module.mutation_rate_limiter._hits.clear()


@pytest.fixture
def app_factory(monkeypatch):
    def build(settings=None):
        settings = settings or make_settings()
        monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
        monkeypatch.setattr(tg_module, "get_settings", lambda: settings)
        app = FastAPI()
        app.include_router(telegram_router)
        app.include_router(email_router)
        app.include_router(orders_router)
        app.include_router(aliases_router)

        @app.get("/health")
        async def health() -> dict:
            return {"status": "ok"}

        @app.get("/")
        async def root() -> dict:
            return {"service": "test"}

        app.state.pipeline = FakePipeline()
        app.state.alias_store = type(
            "S",
            (),
            {
                "create_product": lambda *a, **k: type("R", (), {"model_dump": lambda self: {"id": 1}})(),
                "create_customer": lambda *a, **k: type("R", (), {"model_dump": lambda self: {"id": 2}})(),
            },
        )()
        app.state.catalog = type("C", (), {"get": staticmethod(lambda pid: {"id": pid})})()
        app.state.odoo = type("O", (), {"enabled": True, "get_partner": staticmethod(lambda pid: {"id": pid})})()
        app.state.email_handler = type("E", (), {"process_raw_email": lambda self, raw: [], "poll": lambda self: 0})()
        app.state.telegram_app = None
        return TestClient(app)

    return build


def test_endpoints_open_when_token_unset(app_factory):
    client = app_factory(make_settings(api_auth_token="", telegram_webhook_secret="tgsecret"))
    assert client.get("/orders").status_code == 200


def test_missing_credentials_rejected(app_factory):
    client = app_factory()
    assert client.post("/orders/abc/confirm").status_code == 401
    assert client.get("/orders").status_code == 401


def test_wrong_bearer_rejected(app_factory):
    client = app_factory()
    response = client.get("/orders", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_valid_bearer_allowed(app_factory):
    client = app_factory()
    response = client.post("/orders/abc/confirm", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json()["status"] == "success"


def test_valid_api_key_header_allowed(app_factory):
    client = app_factory()
    assert client.get("/orders", headers={"X-API-Key": TOKEN}).status_code == 200


def test_invalid_api_key_header_rejected(app_factory):
    client = app_factory()
    assert client.get("/orders", headers={"X-API-Key": "wrong"}).status_code == 401


def test_alias_create_requires_auth_then_succeeds(app_factory):
    client = app_factory()
    payload = {"raw_alias": "bread", "target_product_id": 5}
    assert client.post("/aliases/products", json=payload).status_code == 401
    ok = client.post("/aliases/products", json=payload, headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 201


def test_customer_alias_requires_auth_then_succeeds(app_factory):
    client = app_factory()
    payload = {"raw_alias": "abc", "target_partner_id": 42}
    assert client.post("/aliases/customers", json=payload).status_code == 401
    ok = client.post("/aliases/customers", json=payload, headers={"X-API-Key": TOKEN})
    assert ok.status_code == 201


def test_email_webhook_requires_auth_then_processes(app_factory):
    client = app_factory()
    payload = {"raw_email": "Subject: x\n\nbody"}
    assert client.post("/email/webhook", json=payload).status_code == 401
    assert client.post("/email/poll").status_code == 401
    ok = client.post("/email/webhook", json=payload, headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200


def test_telegram_setup_requires_auth(app_factory):
    client = app_factory()
    assert client.post("/telegram/webhook/setup").status_code == 401
    # Auth passes, downstream guard reports missing bot config.
    gated_ok = client.post("/telegram/webhook/setup", headers={"Authorization": f"Bearer {TOKEN}"})
    assert gated_ok.status_code == 503


def test_telegram_delivery_webhook_not_gated_by_api_key(app_factory):
    client = app_factory()
    bad = client.post("/telegram/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    assert bad.status_code == 403
    good = client.post(
        "/telegram/webhook",
        json={},
        headers={"X-Telegram-Bot-Api-Secret-Token": "tgsecret"},
    )
    assert good.status_code == 503  # reached handler: bot not configured


def test_health_and_root_stay_public(app_factory):
    client = app_factory()
    assert client.get("/health").json()["status"] == "ok"
    root = client.get("/")
    assert root.status_code == 200


def test_rate_limit_blocks_and_recovers_dynamically(app_factory):
    limited = make_settings(api_rate_limit_per_minute=2, api_auth_token="")
    client = app_factory(limited)
    for _ in range(2):
        assert client.post("/email/poll").status_code == 200
    blocked = client.post("/email/poll")
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1

    unlimited = make_settings(api_rate_limit_per_minute=0, api_auth_token="")
    fresh_client = app_factory(unlimited)
    assert fresh_client.post("/email/poll").status_code == 200


def test_cors_preflight_for_configured_origin():
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI()
    settings = make_settings(api_cors_origins="https://staff.example.com")
    auth_module.apply_cors(app, settings)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    client = TestClient(app)
    preflight = client.options(
        "/ping",
        headers={
            "Origin": "https://staff.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.headers.get("access-control-allow-origin") == "https://staff.example.com"


def test_cors_absent_when_unconfigured(app_factory):
    client = app_factory()
    preflight = client.options(
        "/orders",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in preflight.headers
