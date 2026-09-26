"""Phase 11 hardening: Odoo XML-RPC timeout, transient retries, auth metrics."""
from __future__ import annotations

import http.client
import xmlrpc.client

import pytest

from order_parser.core.metrics import REGISTRY
from order_parser.integrations import odoo_client as oc
from order_parser.integrations.odoo_client import OdooClient, _TimeoutTransport


class FakeProxy:
    """Stands in for xmlrpc ServerProxy endpoints."""

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
        return behavior

    def authenticate(self, db, user, password, ctx):
        self.calls.append(("authenticate", db))
        result = self._respond("authenticate")
        if isinstance(result, Exception):
            raise result
        return result

    def execute_kw(self, db, uid, password, model, method, args, kwargs):
        self.calls.append((model, method))
        result = self._respond((model, method))
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Capture backoff sleeps so retries are instant in tests."""
    records: list[float] = []
    monkeypatch.setattr(oc, "_sleep", records.append)
    yield records


@pytest.fixture(autouse=True)
def _isolate():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


def make_client(**overrides) -> OdooClient:
    defaults = dict(url="http://odoo.test", db="db1", username="u", password="p")
    defaults.update(overrides)
    return OdooClient(**defaults)


# ------------------------------------------------------------------ transport


@pytest.mark.usefixtures("_isolate")
def test_transport_sets_socket_timeout_http(monkeypatch):
    captured = {}

    class FakeHTTPConnection:
        def __init__(self, host, timeout=None):
            captured.update(host=host, timeout=timeout)

    monkeypatch.setattr(http.client, "HTTPConnection", FakeHTTPConnection)
    conn = _TimeoutTransport(7.5, "http").make_connection("odoo.test:8069")
    assert isinstance(conn, FakeHTTPConnection)
    assert captured == {"host": "odoo.test:8069", "timeout": 7.5}


@pytest.mark.usefixtures("_isolate")
def test_https_url_uses_https_connection_with_timeout(monkeypatch):
    used = {}

    class FakeHTTPSConnection:
        def __init__(self, host, timeout=None):
            used.update(cls="https", timeout=timeout)

    monkeypatch.setattr(http.client, "HTTPSConnection", FakeHTTPSConnection)
    client = make_client(url="https://odoo.secure", timeout=11.0)
    assert client._scheme == "https"
    # NOTE: access .transport on the transport class itself — ServerProxy
    # turns unknown attributes into remote calls.
    conn = _TimeoutTransport(client.timeout, client._scheme).make_connection("odoo.secure")
    assert conn is not None
    assert used == {"cls": "https", "timeout": 11.0}


@pytest.mark.usefixtures("_isolate")
def test_client_reads_resilience_settings(monkeypatch):
    from order_parser.config import Settings

    settings = Settings(
        odoo_timeout_seconds=5.5,
        odoo_max_attempts=4,
        odoo_retry_backoff_seconds=0.25,
    )
    monkeypatch.setattr(oc, "get_settings", lambda: settings)
    client = OdooClient()
    assert (client.timeout, client.max_attempts, client.backoff_seconds) == (5.5, 4, 0.25)


@pytest.mark.usefixtures("_isolate")
def test_constructor_overrides_beat_settings():
    client = make_client(timeout=2.0, max_attempts=5, backoff_seconds=0.5)
    assert (client.timeout, client.max_attempts, client.backoff_seconds) == (2.0, 5, 0.5)


@pytest.mark.usefixtures("_isolate")
def test_min_attempts_is_at_least_one():
    assert make_client(max_attempts=0).max_attempts == 1


# --------------------------------------------------------------- retry logic


@pytest.mark.usefixtures("_isolate")
def test_execute_kw_retries_transient_then_succeeds(sleeps):
    state = {"n": 0}

    def flaky_search():
        state["n"] += 1
        if state["n"] < 3:
            raise ConnectionError("connection reset by peer")
        return [{"id": 1}]

    proxy = FakeProxy().on(("res.partner", "search_read"), flaky_search)
    client = make_client(max_attempts=3)
    client._uid = 2  # skip authenticate path; retries under test are for execute_kw
    client._models = proxy

    result = client.execute_kw("res.partner", "search_read", [[]])

    assert result == [{"id": 1}]
    assert state["n"] == 3
    assert REGISTRY.counter_value("odoo_api_retries_total", outcome="transient") == 2


@pytest.mark.usefixtures("_isolate")
def test_protocol_error_counts_as_transient():
    attempts = {"n": 0}

    def flaky_read():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise xmlrpc.client.ProtocolError("url", 502, "Bad Gateway", {})
        return []

    proxy = FakeProxy().on(("sale.order", "read"), flaky_read)
    client = make_client()
    client._uid = 2
    client._models = proxy

    assert client.execute_kw("sale.order", "read", [[1]]) == []
    assert attempts["n"] == 2
    assert REGISTRY.counter_value("odoo_api_retries_total", outcome="transient") == 1


@pytest.mark.usefixtures("_isolate")
def test_fault_is_never_retried():

    def app_fault():
        raise xmlrpc.client.Fault(1, "Odoo says no")

    proxy = FakeProxy().on("authenticate", app_fault)
    client = make_client()
    client._common = proxy

    with pytest.raises(xmlrpc.client.Fault):
        client.authenticate()

    assert proxy.call_count == 1
    assert REGISTRY.counter_value("odoo_api_retries_total") == 0


@pytest.mark.usefixtures("_isolate")
def test_exhaustion_reraises_last_transient_exception(sleeps):
    def wedged():
        raise TimeoutError("socket timed out")

    proxy = FakeProxy().on("authenticate", wedged)
    client = make_client(max_attempts=3, backoff_seconds=1.5)
    client._common = proxy

    with pytest.raises(TimeoutError):
        client.authenticate()

    assert proxy.call_count == 3
    assert REGISTRY.counter_value("odoo_api_retries_total", outcome="exhausted") == 1
    assert sleeps == [0.0, 1.5]


@pytest.mark.usefixtures("_isolate")
def test_backoff_delays_are_linear(sleeps):

    def down():
        raise OSError("network unreachable")

    proxy = FakeProxy().on("authenticate", down)
    client = make_client(max_attempts=4, backoff_seconds=2.0)
    client._common = proxy

    with pytest.raises(OSError):
        client.authenticate()

    assert sleeps == [0.0, 2.0, 4.0]


# ------------------------------------------------------------------ auth path


@pytest.mark.usefixtures("_isolate")
def test_auth_failure_raises_and_counts():
    proxy = FakeProxy().on("authenticate", False)
    client = make_client()
    client._common = proxy

    with pytest.raises(ConnectionError, match="authentication failed"):
        client.authenticate()

    assert REGISTRY.counter_value("odoo_auth_failures_total") == 1
    assert client._uid is None


@pytest.mark.usefixtures("_isolate")
def test_uid_cached_after_success():
    proxy = FakeProxy().on("authenticate", 42)
    client = make_client()
    client._common = proxy

    assert client.authenticate() == 42
    assert client.authenticate() == 42

    assert proxy.call_count == 1, "second authenticate must reuse cached uid"


@pytest.mark.usefixtures("_isolate")
def test_auth_transient_retry_recovers():
    state = {"n": 0}

    def flaky_auth():
        state["n"] += 1
        if state["n"] == 1:
            raise ConnectionError("reset")
        return 7

    proxy = FakeProxy().on("authenticate", flaky_auth)
    client = make_client()
    client._common = proxy

    assert client.authenticate() == 7
    assert state["n"] == 2
