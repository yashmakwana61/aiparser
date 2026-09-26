import pytest

from order_parser.ai.vision.google_vision_client import GoogleVisionClient, GoogleVisionError
from order_parser.ai.vision.ocr_service import OCRResult, VisionOCRService


def make_response(text: str, confidences: list[float]) -> dict:
    return {
        "responses": [
            {
                "fullTextAnnotation": {
                    "text": text,
                    "pages": [{"confidence": c} for c in confidences],
                }
            }
        ]
    }


class FakeClient:
    provider = "google_vision"

    def __init__(self, configured=True, response=None, error: Exception | None = None):
        self._configured = configured
        self._response = response
        self._error = error
        self.calls: list[tuple[bytes, str]] = []

    @property
    def enabled(self) -> bool:
        return True

    @property
    def configured(self) -> bool:
        return self._configured

    def annotate(self, image_bytes, mime_type="image/png"):
        self.calls.append((image_bytes, mime_type))
        if self._error:
            raise self._error
        return self._response


def test_extracts_text_and_metadata():
    client = FakeClient(response=make_response("BREAD 20", [0.95, 0.85]))
    service = VisionOCRService(client=client)
    assert service.enabled is True
    result = service.extract_text(b"png", filename="a.png")
    assert isinstance(result, OCRResult)
    assert result.text == "BREAD 20"
    assert result.metadata["provider"] == "google_vision"
    assert result.metadata["pages"] == 2
    assert result.metadata["confidence"] == round((0.95 + 0.85) / 2 * 100, 1)
    assert client.calls[0] == (b"png", "image/png")


def test_disabled_when_client_not_configured():
    service = VisionOCRService(client=FakeClient(configured=False))
    assert service.enabled is False


def test_empty_annotation_yields_empty_text():
    client = FakeClient(response={"responses": [{"fullTextAnnotation": {}}]})
    service = VisionOCRService(client=client)
    result = service.extract_text(b"png")
    assert result.text == ""
    assert result.metadata["confidence"] is None


def test_error_propagates_as_ocerror():
    from order_parser.ai.vision.ocr_service import OCRError

    client = FakeClient(error=GoogleVisionError("vision_http_503", retryable=True))
    service = VisionOCRService(client=client)
    with pytest.raises(OCRError):
        service.extract_text(b"png")


def test_default_client_unconfigured(monkeypatch):
    # Force "no key" regardless of the developer's real .env, which may
    # legitimately configure Google Vision (Settings also reads .env).
    from order_parser.config import get_settings

    monkeypatch.setenv("GOOGLE_VISION_API_KEY", "")
    monkeypatch.setenv("ENABLE_GOOGLE_VISION", "false")
    get_settings.cache_clear()
    try:
        assert VisionOCRService().enabled is False
    finally:
        get_settings.cache_clear()
