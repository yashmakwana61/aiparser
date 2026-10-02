"""Resilience: Puter gateway retries (legacy compat), parser JSON-recovery and
NuExtract/Ollama transport retries (production text path)."""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from order_parser.ai import puter as pu
from order_parser.ai import text_parser as text_module
from order_parser.ai import vision_parser as vision_module
from order_parser.ai.puter import PuterError, puter_chat
from order_parser.ai.text_parser import NuExtractError, TextParser, parse_order_with_nuextract
from order_parser.ai.vision_parser import VisionParser
from order_parser.core.metrics import REGISTRY


RAW_JSON = (
    '{"customer": {"name": "ACME"}, '
    '"items": [{"product_name": "Keyboard", "quantity": 2}], '
    '"confidence": 90}'
)

ARGS = {"model": "gpt-4.1", "messages": [{"role": "user", "content": "hi"}]}

GATEWAY_BODY = json.dumps({"result": {"message": {"content": RAW_JSON}}})


class FakeSettings:
    puter_auth_token = "tok"
    ai_drivers_url = "https://api.puter.test/drivers/call"
    ai_timeout_seconds = 9.0
    ai_max_attempts = 3
    ai_retry_backoff_seconds = 2.0


class RecordingGateway:
    """Stands in for urlopen; replays queued outcomes per call."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, request, timeout=None):
        self.calls.append({"timeout": timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        body = outcome.encode("utf-8") if isinstance(outcome, str) else outcome
        return io.BytesIO(body)


def http_error(code: int, body: str = "upstream problem") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.puter.test/drivers/call",
        code,
        "err",
        {},
        io.BytesIO(body.encode("utf-8")),
    )


@pytest.fixture(autouse=True)
def sleeps():
    """Captured backoff sleeps; monkeypatched onto all _sleep seams by _isolate."""
    return _isolate_sleeps


_isolate_sleeps: list[float] = []


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    REGISTRY.reset()
    _isolate_sleeps.clear()
    monkeypatch.setattr(pu, "_sleep", _isolate_sleeps.append)
    monkeypatch.setattr(text_module, "_sleep", _isolate_sleeps.append)
    monkeypatch.setattr(vision_module, "_sleep", _isolate_sleeps.append)
    monkeypatch.setattr(pu, "get_settings", lambda: FakeSettings())
    yield
    REGISTRY.reset()


# ----------------------------------------------------------------- gateway


@pytest.mark.usefixtures("_isolate")
def test_gateway_uses_configured_timeout():
    gateway = RecordingGateway([GATEWAY_BODY])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pu.urllib.request, "urlopen", gateway)
        assert puter_chat(ARGS) == RAW_JSON
    assert gateway.calls == [{"timeout": 9.0}]


@pytest.mark.usefixtures("_isolate")
def test_network_error_is_retried_then_succeeds():
    gateway = RecordingGateway(
        [urllib.error.URLError("connection reset"), GATEWAY_BODY]
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pu.urllib.request, "urlopen", gateway)
        assert puter_chat(ARGS) == RAW_JSON
    assert len(gateway.calls) == 2
    assert REGISTRY.counter_value("ai_api_retries_total", outcome="transient") == 1


@pytest.mark.usefixtures("_isolate")
def test_retryable_http_status_is_retried():
    gateway = RecordingGateway(
        [http_error(502), json.dumps({"result": {"message": {"content": "ok"}}})]
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pu.urllib.request, "urlopen", gateway)
        assert puter_chat(ARGS) == "ok"
    assert REGISTRY.counter_value("ai_api_retries_total", outcome="transient") == 1


@pytest.mark.usefixtures("_isolate")
def test_malformed_body_is_retried():
    gateway = RecordingGateway(
        ["<html>cloudflare</html>", json.dumps({"result": {"message": {"content": "fine"}}})]
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pu.urllib.request, "urlopen", gateway)
        assert puter_chat(ARGS) == "fine"
    assert REGISTRY.counter_value("ai_api_retries_total", outcome="transient") == 1


@pytest.mark.usefixtures("_isolate")
def test_auth_error_fails_immediately_without_retry():
    gateway = RecordingGateway([http_error(401, "bad token")])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pu.urllib.request, "urlopen", gateway)
        with pytest.raises(PuterError, match="HTTP 401"):
            puter_chat(ARGS)
    assert len(gateway.calls) == 1
    assert REGISTRY.counter_value("ai_api_retries_total") == 0


@pytest.mark.usefixtures("_isolate")
def test_exhaustion_raises_after_all_attempts(sleeps):
    gateway = RecordingGateway([urllib.error.URLError("down")] * 3)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pu.urllib.request, "urlopen", gateway)
        with pytest.raises(PuterError, match="after 3 attempts"):
            puter_chat(ARGS)
    assert len(gateway.calls) == 3
    assert REGISTRY.counter_value("ai_api_retries_total", outcome="exhausted") == 1
    assert sleeps == [0.0, 2.0]


@pytest.mark.usefixtures("_isolate")
def test_missing_token_fails_before_any_http_call():
    class NoToken(FakeSettings):
        puter_auth_token = ""

    monkeypatch_settings = NoToken
    called = {"n": 0}

    def boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("network must not be touched")

    original = pu.get_settings
    pu.get_settings = lambda: monkeypatch_settings()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pu.urllib.request, "urlopen", boom)
            with pytest.raises(PuterError, match="PUTER_AUTH_TOKEN"):
                puter_chat(ARGS)
    finally:
        pu.get_settings = original
    assert called["n"] == 0


@pytest.mark.usefixtures("_isolate")
def test_empty_content_not_retried():
    gateway = RecordingGateway([json.dumps({"result": {"message": {"content": None}}})])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pu.urllib.request, "urlopen", gateway)
        with pytest.raises(PuterError, match="no message content"):
            puter_chat(ARGS)
    assert len(gateway.calls) == 1


# ----------------------------------------------------------------- parsers


class NuextractSettings:
    ollama_base_url = "http://127.0.0.1:11434"
    nuextract_model = "nuextract"
    nuextract_timeout_seconds = 30.0
    nuextract_max_attempts = 3
    nuextract_retry_backoff_seconds = 2.0
    nuextract_num_predict = 800
    nuextract_num_ctx = 4096
    nuextract_max_input_chars = 8000
    enable_circuit_breakers = False


@pytest.mark.usefixtures("_isolate")
def test_text_parser_recovers_from_invalid_json():
    responses = iter(["sorry, here is the order:", RAW_JSON])
    calls = {"n": 0}

    def transport(model, prompt):
        calls["n"] += 1
        assert "Template:" in prompt and "Text:" in prompt
        return next(responses)

    parser = TextParser(client=transport)
    result = parser.parse("2 keyboards to ACME")
    assert result["items"][0]["product_name"] == "Keyboard"
    assert calls["n"] == 2
    assert REGISTRY.counter_value("ai_api_retries_total", outcome="invalid_json") == 1


@pytest.mark.usefixtures("_isolate")
def test_text_parser_gives_up_after_max_invalid_attempts():
    calls = {"n": 0}

    def transport(model, prompt):
        calls["n"] += 1
        return "not json at all"

    parser = TextParser(client=transport)
    with pytest.raises(NuExtractError, match="NUEXTRACT_INVALID_JSON"):
        parser.parse("hello")
    assert calls["n"] == 3


@pytest.mark.usefixtures("_isolate")
def test_vision_parser_recovers_from_truncated_json():
    truncated = RAW_JSON[: len(RAW_JSON) // 2]
    responses = iter([truncated, RAW_JSON])
    calls = {"n": 0}

    def transport(args):
        calls["n"] += 1
        return next(responses)

    parser = VisionParser(client=transport)
    result = parser.parse([b"img-bytes"], filename="o.png")
    assert result["customer"]["name"] == "ACME"
    assert calls["n"] == 2


@pytest.mark.usefixtures("_isolate")
def test_parser_does_not_retry_transport_errors():
    calls = {"n": 0}

    def transport(model, prompt):
        calls["n"] += 1
        raise PuterError("gateway down")

    parser = TextParser(client=transport)
    with pytest.raises(PuterError):
        parser.parse("hello")
    assert calls["n"] == 1, "network retries belong to the gateway client"


@pytest.mark.usefixtures("_isolate")
def test_max_attempts_one_disables_json_retry(monkeypatch):
    class OneShot(NuextractSettings):
        nuextract_max_attempts = 1

    calls = {"n": 0}

    def transport(model, prompt):
        calls["n"] += 1
        return "garbage"

    monkeypatch.setattr(text_module, "get_settings", lambda: OneShot())
    parser = TextParser(client=transport)
    assert parser.max_attempts == 1
    with pytest.raises(NuExtractError, match="NUEXTRACT_INVALID_JSON"):
        parser.parse("hello")
    assert calls["n"] == 1


# ------------------------------------------------- nuextract ollama transport


@pytest.mark.usefixtures("_isolate")
def test_nuextract_transient_connection_error_retried_then_succeeds(monkeypatch, sleeps):
    monkeypatch.setattr(text_module, "get_settings", lambda: NuextractSettings())
    with patch("ollama.generate") as mock_generate:
        mock_generate.side_effect = [
            ConnectionError("refused"),
            {"response": RAW_JSON},
        ]
        result = parse_order_with_nuextract("2 keyboards to ACME")
    assert result["items"][0]["product_name"] == "Keyboard"
    assert mock_generate.call_count == 2
    assert (
        REGISTRY.counter_value(
            "nuextract_retries_total",
            model="nuextract",
            outcome="transient",
        )
        == 1
    )
    assert REGISTRY.counter_value("nuextract_success_total", model="nuextract") == 1
    assert sleeps == [0.0]


@pytest.mark.usefixtures("_isolate")
def test_nuextract_exhaustion_raises_unavailable(monkeypatch, sleeps):
    monkeypatch.setattr(text_module, "get_settings", lambda: NuextractSettings())
    with patch("ollama.generate", side_effect=ConnectionError("down")) as mock_generate:
        with pytest.raises(NuExtractError, match="NUEXTRACT_UNAVAILABLE"):
            parse_order_with_nuextract("hello")
    assert mock_generate.call_count == 3
    assert REGISTRY.counter_value("nuextract_failure_total", model="nuextract") == 1
    assert sleeps == [0.0, 2.0]


@pytest.mark.usefixtures("_isolate")
def test_nuextract_timeout_is_controlled(monkeypatch):
    monkeypatch.setattr(text_module, "get_settings", lambda: NuextractSettings())
    with patch("ollama.generate", side_effect=TimeoutError("timed out")):
        with pytest.raises(NuExtractError, match="NUEXTRACT_TIMEOUT"):
            parse_order_with_nuextract("hello")
    assert REGISTRY.counter_value("nuextract_timeout_total", model="nuextract") == 3
    assert REGISTRY.counter_value("nuextract_failure_total", model="nuextract") == 1


@pytest.mark.usefixtures("_isolate")
def test_nuextract_custom_endpoint_uses_explicit_client(monkeypatch):
    class DockerSettings(NuextractSettings):
        ollama_base_url = "http://host.docker.internal:11434"

    monkeypatch.setattr(text_module, "get_settings", lambda: DockerSettings())
    with (
        patch("ollama.generate", side_effect=AssertionError("default client must not be used")),
        patch("ollama.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.generate.return_value = {"response": RAW_JSON}
        result = parse_order_with_nuextract("2 keyboards to ACME")

    assert mock_client_cls.call_count == 1
    assert mock_client_cls.call_args.kwargs["host"] == "http://host.docker.internal:11434"
    generate = mock_client_cls.return_value.generate
    assert generate.call_count == 1
    assert generate.call_args.kwargs["model"] == "nuextract"
    assert generate.call_args.kwargs["options"]["temperature"] == 0.0
    assert "<|end-output|>" in generate.call_args.kwargs["options"]["stop"]
    assert "Template:" in generate.call_args.kwargs["prompt"]
    assert "<|output|>" in generate.call_args.kwargs["prompt"]
    assert result["customer"]["name"] == "ACME"
