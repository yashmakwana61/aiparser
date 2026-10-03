"""Puter text normalisation (ChatGPT) + legacy VisionParser compatibility tests.

Google Vision remains the OCR layer; the ChatGPT text model (via the Puter
gateway) normalizes already-extracted text into structured order JSON.
Unit tests always mock the gateway and never touch the network.
"""
from __future__ import annotations

import pytest

from order_parser.ai.text_parser import TextParser
from order_parser.ai.vision_parser import VisionParser

RAW_JSON = (
    '{"customer": {"name": "ACME"}, '
    '"items": [{"product_name": "Keyboard", "quantity": 2}], '
    '"confidence": 90}'
)

ORDER_TEXT = """Order from ABC Foods Pvt Ltd.

Please supply:
50 Coke 250ml at 32 each
30 Pepsi 500ml at 35 each

GSTIN: 24ABCDE1234F1Z5
PO Number: PO-2026-001
"""

ORDER_JSON = (
    '{"customer": {"name": "ABC Foods Pvt Ltd", "gstin": "24ABCDE1234F1Z5"}, '
    '"items": [{"product_name": "Coke 250ml", "quantity": 50, "unit_price": 32}, '
    '{"product_name": "Pepsi 500ml", "quantity": 30, "unit_price": 35}], '
    '"confidence": 95, "missing_fields": []}'
)


def test_text_parser_sends_prompt_to_transport():
    captured = {}

    def fake_transport(args):
        captured["args"] = args
        return RAW_JSON

    parser = TextParser(client=fake_transport)
    result = parser.parse("please send 2 keyboard to ACME")
    assert result["items"][0]["product_name"] == "Keyboard"
    assert captured["args"]["model"] == parser.model
    messages = captured["args"]["messages"]
    prompt = messages[0]["content"]
    assert "{{CONTENT}}" not in prompt
    assert "2 keyboard to ACME" in prompt


def test_text_parser_normalises_order_with_gpt41():
    """Production shape: full order text in, structured order JSON out."""
    captured = {}

    def fake_transport(args):
        captured["args"] = args
        return ORDER_JSON

    result = TextParser(client=fake_transport).parse(ORDER_TEXT)
    args = captured["args"]
    assert args["model"] == "gpt-4.1"
    assert args["temperature"] == 0
    prompt = args["messages"][0]["content"]
    assert "{{CONTENT}}" not in prompt
    assert "ABC Foods Pvt Ltd" in prompt
    assert "Coke 250ml" in prompt

    assert result["customer"]["name"] == "ABC Foods Pvt Ltd"
    assert result["customer"]["gstin"] == "24ABCDE1234F1Z5"
    assert len(result["items"]) == 2
    assert result["items"][0] == {
        "product_name": "Coke 250ml",
        "quantity": 50,
        "unit_price": 32,
    }
    assert result["items"][1]["quantity"] == 30


def test_text_parser_uses_configured_model_name(monkeypatch):
    from order_parser.ai import text_parser as text_module

    class CustomModel:
        puter_auth_token = "tok"
        ai_text_model = "custom/gpt-test"
        ai_max_attempts = 3
        ai_retry_backoff_seconds = 0.0

    monkeypatch.setattr(text_module, "get_settings", lambda: CustomModel())
    captured = {}

    def fake_transport(args):
        captured["args"] = args
        return RAW_JSON

    TextParser(client=fake_transport).parse("hello")
    assert captured["args"]["model"] == "custom/gpt-test"


def test_text_parser_invalid_json_raises_value_error():
    def fake_transport(args):
        return "This is not JSON at all {{{"

    with pytest.raises(ValueError, match="not valid JSON"):
        TextParser(client=fake_transport).parse("2 keyboards to ACME")


def test_text_parser_empty_input_still_prompts():
    """Empty input is sent (gateway decides); only transport errors fail."""
    captured = {}

    def fake_transport(args):
        captured["args"] = args
        return RAW_JSON

    result = TextParser(client=fake_transport).parse("   ")
    assert result["customer"]["name"] == "ACME"
    assert "messages" in captured["args"]


def test_text_parser_output_feeds_normalizer():
    import json

    from order_parser.normalizers.order_normalizer import OrderNormalizer

    parsed = json.loads(ORDER_JSON)
    order = OrderNormalizer.normalize(parsed, source="", input_type="text")
    assert order.customer.name == "ABC Foods Pvt Ltd"
    assert order.customer.gstin == "24ABCDE1234F1Z5"
    assert len(order.items) == 2
    assert order.items[0].quantity == 50


def test_vision_parser_sends_images_as_data_urls():
    captured = {}

    def fake_transport(args):
        captured["args"] = args
        return RAW_JSON

    parser = VisionParser(client=fake_transport)
    result = parser.parse([b"fake-image-bytes"], filename="order.png")
    assert result["customer"]["name"] == "ACME"
    content = captured["args"]["messages"][0]["content"]
    assert any(part["type"] == "text" for part in content)
    image_part = next(part for part in content if part["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_parser_requires_token_without_client(monkeypatch):
    from order_parser.ai import puter

    class FakeSettings:
        puter_auth_token = ""
        ai_drivers_url = "https://api.puter.com/drivers/call"

    monkeypatch.setattr(puter, "get_settings", lambda: FakeSettings())
    with pytest.raises(RuntimeError, match="PUTER_AUTH_TOKEN"):
        puter.puter_chat({"model": "gpt-4.1", "messages": [{"role": "user", "content": "hi"}]})


def test_settings_puter_defaults():
    from order_parser.config import get_settings

    settings = get_settings()
    assert settings.ai_text_model == "gpt-4.1"
    assert settings.ai_vision_model == "gpt-4o"
    assert "puter.com" in settings.ai_base_url
    assert settings.ai_drivers_url.endswith("/drivers/call")