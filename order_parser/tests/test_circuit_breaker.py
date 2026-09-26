"""Phase 15 hardening: circuit breakers for Odoo and the AI gateway."""
from __future__ import annotations

import io
import urllib.error

import pytest

from order_parser.ai import puter as pu
from order_parser.ai.puter import PuterError, puter_chat
from order_parser.config import Settings
from order_parser.core import metrics
from order_parser.core.breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitOpenError,
    get_dependency_breaker,
    reset_breakers,
)
from order_parser.core.metrics import REGISTRY
from order_parser.integrations import odoo_client as oc
from order_parser.integrations.odoo_client import OdooClient


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProxy:
    def __init__(self):
        self.calls: list[tuple] = []
        self.behaviors: dict = {}

    def on(self, key, value):
        self.behaviors[key] = value
        return self

    def _respond(self, key):
        behavior = self.behaviors.get(key)
        if callable(behavior):
            return behavior()
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    def authenticate(self, db, user, password, ctx):
        self.calls.append("authenticate")
        return self._respond("authenticate")

    def execute_kw(self, db, uid, password, model, method, args, kwargs):
        self.calls.append((model, method))
        return self._respond((model, method))


@pytest.fixture(autouse=True)
def _isolate():
    REGISTRY.reset()
    reset_breakers()
    yield
    reset_breakers()
    REGISTRY.reset()


# ------------------------------------------------------------ breaker unit


@pytest.mark.usefixtures("_isolate")
def test_closed_circuit_allows_all_requests():
    clock = FakeClock()
    breaker = CircuitBreaker("t", failure_threshold=2, recovery_seconds=10, clock=clock)

    assert breaker.state == CLOSED
    assert breaker.allow() is True
    assert breaker.allow() is True


@pytest.mark.usefixtures("_isolate")
def test_opens_after_threshold_consecutive_failures():
    clock = FakeClock()
    breaker = CircuitBreaker("t", failure_threshold=3, recovery_seconds=10, clock=clock)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.state == CLOSED
    breaker.record_failure()

    assert breaker.state == OPEN


@pytest.mark.usefixtures("_isolate")
def test_open_circuit_rejects_and_counts():
    clock = FakeClock()
    breaker = CircuitBreaker("t", failure_threshold=1, recovery_seconds=10, clock=clock)
    breaker.record_failure()

    assert breaker.allow() is False
    assert breaker.allow() is False
    assert REGISTRY.counter_value("circuit_requests_rejected_total", circuit="t") == 2


@pytest.mark.usefixtures("_isolate")
def test_success_resets_failure_count_below_threshold():
    clock = FakeClock()
    breaker = CircuitBreaker("t", failure_threshold=3, recovery_seconds=10, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()

    assert breaker.state == CLOSED, "streak was broken by success"


@pytest.mark.usefixtures("_isolate")
def test_half_open_grants_exactly_one_probe_after_cooldown():
    clock = FakeClock()
    breaker = CircuitBreaker("t", failure_threshold=1, recovery_seconds=30, clock=clock)
    breaker.record_failure()
    clock.advance(29)
    assert breaker.allow() is False, "still within cooldown"
    clock.advance(1)

    assert breaker.allow() is True, "first request after cooldown becomes the probe"
    assert breaker.state == HALF_OPEN
    assert breaker.allow() is False, "concurrent probe rejected"


@pytest.mark.usefixtures("_isolate")
def test_successful_probe_closes_circuit():
    clock = FakeClock()
    breaker = CircuitBreaker("t", failure_threshold=1, recovery_seconds=5, clock=clock)
    breaker.record_failure()
    clock.advance(6)
    assert breaker.allow() is True
    breaker.record_success()

    assert breaker.state == CLOSED
    assert breaker.allow() is True
    assert REGISTRY.counter_value("circuit_transitions_total", circuit="t", state="closed") == 1


@pytest.mark.usefixtures("_isolate")
def test_failed_probe_reopens_for_full_new_cycle():
    clock = FakeClock()
    breaker = CircuitBreaker("t", failure_threshold=1, recovery_seconds=5, clock=clock)
    breaker.record_failure()
    clock.advance(6)
    assert breaker.allow() is True
    breaker.record_failure()

    assert breaker.state == OPEN
    clock.advance(4)
    assert breaker.allow() is False, "new cooldown not elapsed"
    clock.advance(1)
    assert breaker.allow() is True


# --------------------------------------------------------- shared registry


@pytest.mark.usefixtures("_isolate")
def test_disabled_settings_yield_no_breaker(monkeypatch):
    settings = Settings(enable_circuit_breakers=False)
    assert get_dependency_breaker("odoo", settings) is None


@pytest.mark.usefixtures("_isolate")
def test_registry_reuses_breaker_until_params_change(monkeypatch):
    first = get_dependency_breaker("odoo", Settings(enable_circuit_breakers=True))
    again = get_dependency_breaker("odoo", Settings(enable_circuit_breakers=True))
    changed = get_dependency_breaker(
        "odoo", Settings(enable_circuit_breakers=True, breaker_failure_threshold=9)
    )
    other = get_dependency_breaker(
        "ai_gateway", Settings(enable_circuit_breakers=True, breaker_failure_threshold=9)
    )
    assert first is again
    assert changed is not first
    assert other is not changed


# -------------------------------------------------------------- odoo client


def make_enabled_settings(**extra) -> Settings:
    defaults = dict(
        odoo_url="http://odoo.test",
        enable_circuit_breakers=True,
        breaker_failure_threshold=2,
        breaker_recovery_seconds=60.0,
    )
    defaults.update(extra)
    return Settings(**defaults)


@pytest.fixture()
def fast_sleep(monkeypatch):
    monkeypatch.setattr(oc, "_sleep", lambda seconds: None)


@pytest.mark.usefixtures("_isolate", "fast_sleep")
def test_disabled_breakers_keep_legacy_retry_behavior(monkeypatch):
    proxy = FakeProxy().on("authenticate", ConnectionError("down"))
    monkeypatch.setattr(oc, "get_settings", lambda: Settings(odoo_url="http://x"))
    client = OdooClient(max_attempts=1)
    client._common = proxy

    for _ in range(5):
        with pytest.raises(ConnectionError):
            client.authenticate()

    assert len(proxy.calls) == 5, "every call must still reach the network"


@pytest.mark.usefixtures("_isolate", "fast_sleep")
def test_odoo_circuit_opens_and_fails_fast(monkeypatch):
    proxy = FakeProxy().on("authenticate", ConnectionError("down"))
    settings = make_enabled_settings()
    monkeypatch.setattr(oc, "get_settings", lambda: settings)
    client = OdooClient(max_attempts=1)
    client._common = proxy

    with pytest.raises(ConnectionError):
        client.authenticate()
    with pytest.raises(ConnectionError):
        client.authenticate()
    network_calls_after_two = len(proxy.calls)
    assert network_calls_after_two == 2

    with pytest.raises(CircuitOpenError, match="circuit open"):
        client.authenticate()

    assert len(proxy.calls) == network_calls_after_two, "open circuit must not touch network"
    assert REGISTRY.counter_value("circuit_requests_rejected_total", circuit="odoo") == 1


@pytest.mark.usefixtures("_isolate", "fast_sleep")
def test_odoo_circuit_recovery_via_single_probe(monkeypatch):
    state = {"fail": True}

    def flaky_auth():
        if state["fail"]:
            raise ConnectionError("down")
        return 42

    proxy = FakeProxy().on("authenticate", flaky_auth)
    settings = make_enabled_settings(breaker_failure_threshold=1, breaker_recovery_seconds=60.0)
    monkeypatch.setattr(oc, "get_settings", lambda: settings)
    client = OdooClient(max_attempts=1)
    client._common = proxy

    # Attach the fake clock BEFORE failures so _opened_at lands on it.
    breaker = get_dependency_breaker("odoo", settings)
    fake_clock = FakeClock()
    breaker._clock = fake_clock

    with pytest.raises(ConnectionError):
        client.authenticate()
    with pytest.raises(CircuitOpenError):
        client.authenticate()

    fake_clock.advance(61)
    state["fail"] = False

    assert client.authenticate() == 42, "single probe passes through after cooldown"

    # Circuit closed again: subsequent calls go straight through.
    assert client.authenticate() == 42
    assert get_dependency_breaker("odoo", settings).state == CLOSED


# ------------------------------------------------------------- AI gateway


class RecordingGateway:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        result = self.outcome() if callable(self.outcome) else self.outcome
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(result.encode("utf-8"))


ARGS = {"model": "gpt-4.1", "messages": [{"role": "user", "content": "hi"}]}


class GatewaySettings(Settings):
    pass


@pytest.mark.usefixtures("_isolate")
def test_ai_gateway_circuit_opens_and_fails_fast(monkeypatch):
    gateway = RecordingGateway(lambda: (_ for _ in ()).throw(urllib.error.URLError("down")))
    settings = Settings(
        puter_auth_token="tok",
        ai_drivers_url="https://api.puter.test/drivers/call",
        ai_max_attempts=1,
        ai_retry_backoff_seconds=0.0,
        ai_timeout_seconds=9.0,
        enable_circuit_breakers=True,
        breaker_failure_threshold=2,
        breaker_recovery_seconds=60.0,
    )
    monkeypatch.setattr(pu, "get_settings", lambda: settings)
    monkeypatch.setattr(pu.urllib.request, "urlopen", gateway)

    for _ in range(2):
        with pytest.raises(PuterError, match="unavailable"):
            puter_chat(ARGS)
    calls_before = gateway.calls
    assert calls_before == 2

    with pytest.raises(CircuitOpenError):
        puter_chat(ARGS)

    assert gateway.calls == calls_before, "open circuit must not touch network"


@pytest.mark.usefixtures("_isolate")
def test_ai_gateway_recovers_through_probe(monkeypatch):
    outcomes = iter([urllib.error.URLError("down"), urllib.error.URLError("down")])

    def next_outcome():
        try:
            return next(outcomes)
        except StopIteration:
            return '{"result": {"message": {"content": "recovered"}}}'

    gateway = RecordingGateway(next_outcome)
    settings = Settings(
        puter_auth_token="tok",
        ai_drivers_url="https://api.puter.test/drivers/call",
        ai_max_attempts=1,
        ai_retry_backoff_seconds=0.0,
        ai_timeout_seconds=9.0,
        enable_circuit_breakers=True,
        breaker_failure_threshold=2,
        breaker_recovery_seconds=30.0,
    )
    monkeypatch.setattr(pu, "get_settings", lambda: settings)
    monkeypatch.setattr(pu.urllib.request, "urlopen", gateway)

    breaker = get_dependency_breaker("ai_gateway", settings)
    fake_clock = FakeClock()
    breaker._clock = fake_clock

    with pytest.raises(PuterError):
        puter_chat(ARGS)
    with pytest.raises(PuterError):
        puter_chat(ARGS)

    fake_clock.advance(31)

    assert puter_chat(ARGS) == "recovered"
    assert breaker.state == CLOSED
