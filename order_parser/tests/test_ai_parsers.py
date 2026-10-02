"""NuExtract text extraction (Ollama) + legacy VisionParser compatibility tests.

Google Vision remains the OCR layer; NuExtract performs semantic order
extraction from already-extracted text. Unit tests always mock Ollama and
never touch the real local server.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from order_parser.ai.text_parser import (
    NuExtractError,
    TextParser,
    _extract_response_text,
    build_nuextract_prompt,
    build_nuextract_schema,
    parse_order_with_nuextract,
)
from order_parser.ai.vision_parser import VisionParser
from order_parser.core.metrics import REGISTRY

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

NUEXTRACT_ORDER = {
    "customer": {
        "name": "ABC Foods Pvt Ltd",
        "email": "",
        "phone": "",
        "address": "",
        "city": "",
        "state": "",
        "zip_code": "",
        "gstin": "24ABCDE1234F1Z5",
        "country": "",
    },
    "items": [
        {
            "product_name": "Coke 250ml",
            "quantity": 50,
            "unit_price": 32,
            "uom": "",
            "ambiguous": False,
        },
        {
            "product_name": "Pepsi 500ml",
            "quantity": 30,
            "unit_price": 35,
            "uom": "",
            "ambiguous": False,
        },
    ],
    "order_reference": "PO-2026-001",
    "order_date": "",
    "delivery_date": "",
    "billing_address": "",
    "shipping_address": "",
    "currency": "",
    "notes": "",
    "confidence": 95,
    "missing_fields": [],
}


@pytest.fixture(autouse=True)
def _clean_metrics():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


def _ollama_result(payload) -> dict:
    return {"response": json.dumps(payload)}


# ------------------------------------------------------- canonical pipeline


def test_nuextract_parsing_pipeline():
    with patch("ollama.generate") as mock_generate:
        mock_generate.return_value = _ollama_result(NUEXTRACT_ORDER)
        result = parse_order_with_nuextract(ORDER_TEXT)

    assert mock_generate.call_count == 1
    kwargs = mock_generate.call_args.kwargs
    assert kwargs["model"] == "nuextract"
    assert kwargs["options"]["temperature"] == 0.0
    assert kwargs["options"]["num_predict"] >= 128
    assert kwargs["options"]["num_ctx"] >= 2048
    assert "<|end-output|>" in kwargs["options"]["stop"]
    prompt = kwargs["prompt"]
    assert prompt.startswith("<|input|>\n### Template:\n{")
    assert "Template:" in prompt
    assert "### Text:" in prompt
    assert "Text:" in prompt
    assert prompt.rstrip().endswith("<|output|>")
    assert "ABC Foods Pvt Ltd" in prompt
    assert "Coke 250ml" in prompt

    assert isinstance(result, dict)
    assert result["customer"]["name"] == "ABC Foods Pvt Ltd"
    assert result["customer"]["gstin"] == "24ABCDE1234F1Z5"
    assert result["order_reference"] == "PO-2026-001"
    assert len(result["items"]) == 2
    assert result["items"][0]["product_name"] == "Coke 250ml"
    assert result["items"][0]["quantity"] == 50
    assert result["items"][0]["unit_price"] == 32
    assert result["items"][1]["product_name"] == "Pepsi 500ml"
    assert result["items"][1]["quantity"] == 30
    assert result["items"][1]["unit_price"] == 35


def test_text_parser_parse_delegates_to_nuextract():
    with patch("ollama.generate") as mock_generate:
        mock_generate.return_value = _ollama_result(NUEXTRACT_ORDER)
        result = TextParser().parse(ORDER_TEXT)

    assert mock_generate.call_count == 1
    assert result["customer"]["name"] == "ABC Foods Pvt Ltd"
    assert len(result["items"]) == 2


def test_text_parser_uses_configured_model_name(monkeypatch):
    from order_parser.ai import text_parser as text_module

    class CustomModel:
        ollama_base_url = "http://127.0.0.1:11434"
        nuextract_model = "custom/nuextract-test"
        nuextract_timeout_seconds = 120.0
        nuextract_max_attempts = 3
        nuextract_retry_backoff_seconds = 0.0
        nuextract_num_predict = 800
        nuextract_num_ctx = 4096
        nuextract_max_input_chars = 8000
        enable_circuit_breakers = False

    monkeypatch.setattr(text_module, "get_settings", lambda: CustomModel())
    with patch("ollama.generate") as mock_generate:
        mock_generate.return_value = _ollama_result(NUEXTRACT_ORDER)
        parse_order_with_nuextract(ORDER_TEXT)
    assert mock_generate.call_args.kwargs["model"] == "custom/nuextract-test"


def test_text_parser_injected_client_seam():
    captured = {}

    def fake_client(model, prompt):
        captured["model"] = model
        captured["prompt"] = prompt
        return RAW_JSON

    parser = TextParser(client=fake_client)
    result = parser.parse("please send 2 keyboard to ACME")
    assert result["items"][0]["product_name"] == "Keyboard"
    assert captured["prompt"].startswith("<|input|>")
    assert "Template:" in captured["prompt"]
    assert "Text:" in captured["prompt"]
    assert "2 keyboard to ACME" in captured["prompt"]
    assert captured["model"] == parser.model


# ------------------------------------------------------------------ failures


def test_nuextract_invalid_json_raises_controlled_error():
    with patch("ollama.generate") as mock_generate:
        mock_generate.return_value = {"response": "This is not JSON"}
        with pytest.raises(NuExtractError, match="NUEXTRACT_INVALID_JSON"):
            parse_order_with_nuextract(ORDER_TEXT)


def test_nuextract_non_object_json_raises_schema_error():
    with patch("ollama.generate") as mock_generate:
        mock_generate.return_value = {"response": "[1, 2, 3]"}
        with pytest.raises(NuExtractError, match="NUEXTRACT_INVALID_SCHEMA"):
            parse_order_with_nuextract(ORDER_TEXT)


def test_nuextract_unavailable_raises_without_fabrication():
    from order_parser.ai import text_parser as text_module

    with (
        patch("ollama.generate", side_effect=ConnectionError("refused")) as mock_generate,
        patch.object(text_module, "_sleep", lambda *args: None),
    ):
        with pytest.raises(NuExtractError, match="NUEXTRACT_UNAVAILABLE"):
            parse_order_with_nuextract(ORDER_TEXT)
    # Transient failure is retried within the bounded attempt budget.
    assert mock_generate.call_count == 3


def test_nuextract_empty_text_rejected():
    with patch("ollama.generate") as mock_generate:
        with pytest.raises(NuExtractError, match="NUEXTRACT_EMPTY_INPUT"):
            parse_order_with_nuextract("")
        with pytest.raises(NuExtractError, match="NUEXTRACT_EMPTY_INPUT"):
            parse_order_with_nuextract("   \n  ")
        with pytest.raises(NuExtractError, match="NUEXTRACT_EMPTY_INPUT"):
            parse_order_with_nuextract(None)  # type: ignore[arg-type]
        with pytest.raises(NuExtractError, match="NUEXTRACT_EMPTY_INPUT"):
            TextParser().parse("  ")
    mock_generate.assert_not_called()


def test_nuextract_repeated_blocks_first_object_wins():
    """Production shape: leading space, valid block, <|end-output|>, repeats."""
    stale = json.dumps({"customer": {"name": "STALE"}, "items": []})
    repeated = " " + json.dumps(NUEXTRACT_ORDER) + "\n<|end-output|> " + stale
    with patch("ollama.generate") as mock_generate:
        mock_generate.return_value = {"response": repeated}
        result = parse_order_with_nuextract(ORDER_TEXT)
    assert result["customer"]["name"] == "ABC Foods Pvt Ltd"
    assert len(result["items"]) == 2


def test_nuextract_options_builder():
    from order_parser.ai.text_parser import build_nuextract_options
    from order_parser.config import Settings

    options = build_nuextract_options(Settings())
    assert options["temperature"] == 0.0
    assert options["num_predict"] == 800
    assert options["num_ctx"] == 4096
    assert "<|end-output|>" in options["stop"]

    custom = build_nuextract_options(
        Settings(nuextract_num_predict=500, nuextract_num_ctx=8192)
    )
    assert custom["num_predict"] == 500
    assert custom["num_ctx"] == 8192


def test_nuextract_markdown_wrapped_json_is_parsed():
    fenced = "```json\n" + json.dumps(NUEXTRACT_ORDER) + "\n```"
    with patch("ollama.generate") as mock_generate:
        mock_generate.return_value = {"response": fenced}
        result = parse_order_with_nuextract(ORDER_TEXT)
    assert result["customer"]["name"] == "ABC Foods Pvt Ltd"
    assert len(result["items"]) == 2


def test_nuextract_object_response_shape_is_supported():
    with patch("ollama.generate") as mock_generate:
        mock_generate.return_value = SimpleNamespace(response=json.dumps(NUEXTRACT_ORDER))
        result = parse_order_with_nuextract(ORDER_TEXT)
    assert result["order_reference"] == "PO-2026-001"


def test_extract_response_text_shapes():
    assert _extract_response_text({"response": '{"a": 1}'}) == '{"a": 1}'
    assert _extract_response_text(SimpleNamespace(response="hi")) == "hi"
    assert _extract_response_text("raw") == "raw"
    assert _extract_response_text(None) == ""
    assert _extract_response_text({}) == ""
    assert _extract_response_text({"response": None}) == ""


# ------------------------------------------------------------------- schema


def test_nuextract_schema_matches_normalizer_contract():
    schema = build_nuextract_schema()
    assert set(schema["customer"]) == {
        "name",
        "email",
        "phone",
        "address",
        "city",
        "state",
        "zip_code",
        "gstin",
        "country",
    }
    item = schema["items"][0]
    assert item["quantity"] == 0  # "quantity", never "qty"
    assert "qty" not in item
    assert item["unit_price"] is None
    assert item["uom"] == ""
    assert item["ambiguous"] is False
    assert schema["order_reference"] == ""
    assert schema["confidence"] == 0
    assert schema["missing_fields"] == []
    # Template must serialize cleanly for the Template:/Text: prompt.
    prompt = build_nuextract_prompt("hello")
    assert prompt.startswith("<|input|>\n### Template:\n{")
    assert "\n\n### Text:\nhello\n<|output|>\n" in prompt


def test_nuextract_output_feeds_normalizer():
    from order_parser.normalizers.order_normalizer import OrderNormalizer

    order = OrderNormalizer.normalize(dict(NUEXTRACT_ORDER), source="", input_type="text")
    assert order.customer.name == "ABC Foods Pvt Ltd"
    assert order.customer.gstin == "24ABCDE1234F1Z5"
    assert order.order_reference == "PO-2026-001"
    assert len(order.items) == 2
    assert order.items[0].quantity == 50


# -------------------------------------------------------------------- config


def test_settings_nuextract_defaults():
    from order_parser.config import get_settings

    settings = get_settings()
    assert settings.nuextract_model == "nuextract"
    assert settings.ollama_base_url
    assert settings.nuextract_timeout_seconds > 0
    assert settings.nuextract_max_attempts >= 1
    assert settings.nuextract_retry_backoff_seconds >= 0
    assert settings.nuextract_num_predict >= 128
    assert settings.nuextract_num_ctx >= 2048
    assert settings.nuextract_max_input_chars >= 256


# ------------------------------------------- legacy compatibility (retained)


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
