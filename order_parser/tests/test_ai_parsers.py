import pytest

from order_parser.ai.text_parser import TextParser
from order_parser.ai.vision_parser import VisionParser

RAW_JSON = (
    '{"customer": {"name": "ACME"}, '
    '"items": [{"product_name": "Keyboard", "quantity": 2}], '
    '"confidence": 90}'
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