"""Google Vision adapter tests - no network, injected transports only."""
import json

import pytest

from order_parser.ai.vision.google_vision_client import GoogleVisionClient, GoogleVisionError


KEY_URL = f"https://vision.googleapis.com/v1/images:annotate?key=test-key-123"


def ok_transport(expected_features="DOCUMENT_TEXT_DETECTION"):
    calls = []

    def transport(url, body, timeout):
        calls.append({"url": url, "body": json.loads(body), "timeout": timeout})
        return 200, json.dumps({"responses": [{"fullTextAnnotation": {"text": "hello"}}]})

    return transport, calls


def test_payload_and_auth():
    transport, calls = ok_transport()
    client = GoogleVisionClient(api_key="test-key-123", transport=transport)
    result = client.annotate(b"imgbytes", mime_type="image/png")
    assert result["responses"][0]["fullTextAnnotation"]["text"] == "hello"
    request = calls[0]["body"]["requests"][0]
    assert request["features"][0]["type"] == "DOCUMENT_TEXT_DETECTION"
    assert request["imageContext"]["languageHints"] == ["en"]
    assert calls[0]["timeout"] == client.timeout_seconds
    assert "key=test-key-123" in calls[0]["url"]


def test_not_configured_raises_without_network():
    client = GoogleVisionClient(api_key="", transport=ok_transport()[0])
    with pytest.raises(GoogleVisionError) as exc:
        client.annotate(b"x")
    assert not exc.value.retryable


def test_retry_then_success(monkeypatch):
    monkeypatch.setattr("order_parser.ai.vision.google_vision_client.time.sleep", lambda s: None)
    attempts = {"n": 0}

    def flaky(url, body, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return 500, "boom"
        return 200, json.dumps({"responses": [{"fullTextAnnotation": {"text": "ok"}}]})

    client = GoogleVisionClient(api_key="k", max_retries=3, backoff_seconds=0.25, transport=flaky)
    result = client.annotate(b"img")
    assert result["responses"][0]["fullTextAnnotation"]["text"] == "ok"
    assert attempts["n"] == 3


def test_rate_limit_exhausts_retries(monkeypatch):
    monkeypatch.setattr("order_parser.ai.vision.google_vision_client.time.sleep", lambda s: None)
    attempts = {"n": 0}

    def limited(url, body, timeout):
        attempts["n"] += 1
        return 429, "slow down"

    client = GoogleVisionClient(api_key="k", max_retries=2, transport=limited)
    with pytest.raises(GoogleVisionError) as exc:
        client.annotate(b"img")
    assert exc.value.retryable and exc.value.status == 429
    assert attempts["n"] == 3  # initial + 2 retries


def test_non_retryable_http_error():
    def bad_request(url, body, timeout):
        return 400, "bad"

    client = GoogleVisionClient(api_key="k", max_retries=3, transport=bad_request)
    with pytest.raises(GoogleVisionError) as exc:
        client.annotate(b"img")
    assert not exc.value.retryable and exc.value.status == 400


def test_api_error_in_200_body():
    def api_error(url, body, timeout):
        return 200, json.dumps({"error": {"code": 7, "message": "permission denied"}})

    client = GoogleVisionClient(api_key="k", transport=api_error)
    with pytest.raises(GoogleVisionError) as exc:
        client.annotate(b"img")
    assert "permission denied" in str(exc.value)


def test_backoff_is_exponential(monkeypatch):
    delays: list[float] = []
    monkeypatch.setattr("order_parser.ai.vision.google_vision_client.time.sleep", delays.append)

    def failing(url, body, timeout):
        return 503, "unavailable"

    client = GoogleVisionClient(api_key="k", max_retries=3, backoff_seconds=1.0, transport=failing)
    with pytest.raises(GoogleVisionError):
        client.annotate(b"img")
    assert delays == [1.0, 2.0, 4.0]
