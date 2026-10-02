"""Phase 4 processor hardening: image OCR-first flow, PDF routing, Excel layer."""
import hashlib
from pathlib import Path

import pytest

from order_parser.ai.vision.ocr_service import VisionOCRService
from order_parser.ai.vision.google_vision_client import GoogleVisionError, GoogleVisionClient
from order_parser.config import get_settings
from order_parser.core.attachment_store import AttachmentStore
from order_parser.extractors.pdf_extractor import PDFExtractor
from order_parser.processors.image_processor import ImageProcessor
from order_parser.processors.pdf_processor import PDFProcessor


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"rest-of-image" * 4
JPEG_BYTES = b"\xff\xd8\xff" + b"jpeg-data"


class FakeOCR:
    provider = "google_vision"

    def __init__(self, text="Customer ABC\nbread 20", enabled=True, error=None):
        self._enabled = enabled
        self.text = text
        self.error = error
        self.calls: list[bytes] = []

    @property
    def enabled(self):
        return self._enabled

    def extract_text(self, image_bytes, filename="", mime_type="image/png"):
        if self.error:
            raise self.error
        self.calls.append(image_bytes)
        from order_parser.ai.vision.ocr_service import OCRResult

        return OCRResult(text=self.text, metadata={"provider": "google_vision", "pages": 1, "confidence": 91.0})


class FakeTextParser:
    def __init__(self):
        self.received: list[str] = []

    def parse(self, content: str) -> dict:
        self.received.append(content)
        return {"customer": {"name": "ABC"}, "items": [{"product_name": "Bread", "quantity": 20}], "confidence": 90}


class FakeVisionParser:
    def __init__(self):
        self.images_seen: list[list[bytes]] = []

    def parse(self, images, filename=""):
        self.images_seen.append(list(images))
        return {"customer": {"name": "Legacy"}, "items": [{"product_name": "Bread", "quantity": 3}], "confidence": 88}


@pytest.fixture()
def store(tmp_path):
    return AttachmentStore(tmp_path / "uploads")


# ---------------------------------------------------------------- image flow


def test_image_ocr_first_flow(store):
    ocr = FakeOCR(text="Customer ABC\nbread 20\nprice 500")
    parser = FakeTextParser()
    processor = ImageProcessor(parser=FakeVisionParser(), ocr_service=ocr, text_parser=parser, attachment_store=store)
    parsed = processor.process(PNG_BYTES, filename="order.png")

    assert parser.received == ["Customer ABC\nbread 20\nprice 500"]  # AI interpreted OCR text
    assert parsed.order.items[0].product_name == "Bread"
    assert parsed.extracted_text.startswith("Customer ABC")
    meta = parsed.ai_response["attachment"]
    assert meta["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert meta["mime_type"] == "image/png"
    assert meta["ocr"]["provider"] == "google_vision"
    assert Path(meta["path"]).exists()  # original preserved


def test_image_unsupported_type_flagged(store):
    parser = FakeTextParser()
    processor = ImageProcessor(
        parser=FakeVisionParser(), ocr_service=FakeOCR(), text_parser=parser, attachment_store=store
    )
    parsed = processor.process(b"<html>not an image</html>", filename="x.png")
    assert parsed.ai_response["ocr_failed"] is True
    assert parsed.ai_response["error_code"] == "UNSUPPORTED_IMAGE_TYPE"
    assert parser.received == []


def test_image_too_large_flagged(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 0)  # any content exceeds the limit
    processor = ImageProcessor(
        parser=FakeVisionParser(), ocr_service=FakeOCR(), text_parser=FakeTextParser(), attachment_store=store
    )
    parsed = processor.process(PNG_BYTES, filename="big.png")
    assert parsed.ai_response["ocr_failed"] is True
    assert parsed.ai_response["error_code"] == "IMAGE_TOO_LARGE"


def test_image_ocr_failure_never_calls_ai(store):
    ocr = FakeOCR(error=GoogleVisionError("vision_http_503", retryable=True))
    parser = FakeTextParser()
    legacy = FakeVisionParser()
    processor = ImageProcessor(parser=legacy, ocr_service=ocr, text_parser=parser, attachment_store=store)
    parsed = processor.process(JPEG_BYTES, filename="o.jpg")
    assert parser.received == [] and not legacy.images_seen
    assert parsed.order.items == []
    assert parsed.order.metadata.confidence == 0.0
    assert parsed.ai_response["error_code"] == "OCR_UNAVAILABLE"
    assert parsed.ai_response["attachment"]["path"]  # original still preserved


def test_image_legacy_path_when_vision_disabled(store):
    # GPT-vision fallback removed: disabled OCR now flags for review.
    legacy = FakeVisionParser()
    processor = ImageProcessor(
        parser=legacy,
        ocr_service=FakeOCR(enabled=False),
        text_parser=FakeTextParser(),
        attachment_store=store,
    )
    parsed = processor.process(PNG_BYTES, filename="a.png")
    assert not legacy.images_seen
    assert parsed.ai_response["ocr_failed"] is True
    assert parsed.ai_response["error_code"] == "OCR_UNAVAILABLE"


def test_sniffing_rejects_renamed_payloads():
    from order_parser.processors.image_processor import sniff_image_mime

    assert sniff_image_mime(b"GIF89a....") == "image/gif"
    assert sniff_image_mime(b"RIFF1234WEBPVP8 ") == "image/webp"
    assert sniff_image_mime(b"\x00\x00\x00 ftypheic....") == "image/heic"
    assert sniff_image_mime(b"JustText") is None


# ------------------------------------------------------------------ pdf flow


def make_pdf(page_count=1, with_text=True):
    doc = fitz_open()
    for i in range(page_count):
        page = doc.new_page()
        if with_text:
            page.insert_text((72, 72), f"Order for ABC Industries page {i} bread 20 boxes price 450 urgent")
    data = doc.tobytes()
    doc.close()
    return data


def fitz_open():
    import pymupdf

    return pymupdf.open()


def test_pdf_with_usable_text_skips_ocr(store):
    ocr = FakeOCR()
    parser = FakeTextParser()
    processor = PDFProcessor(
        text_parser=parser, vision_parser=FakeVisionParser(), ocr_service=ocr, attachment_store=store
    )
    parsed = processor.process(make_pdf(with_text=True), filename="t.pdf")
    assert ocr.calls == []  # never blindly sent to OCR
    assert parser.received and "ABC Industries" in parser.received[0]
    assert parsed.ai_response["attachment"]["kind"] == "pdf"
    assert parsed.order.metadata.input_type == "pdf"


def test_scanned_pdf_routes_through_ocr(store):
    scanned = make_pdf(with_text=False)
    assert len(PDFExtractor.extract_text(scanned).strip()) < 30
    ocr = FakeOCR(text="scanned page words bread 10")
    parser = FakeTextParser()
    processor = PDFProcessor(
        text_parser=parser, vision_parser=FakeVisionParser(), ocr_service=ocr, attachment_store=store
    )
    parsed = processor.process(scanned, filename="scan.pdf")
    assert len(ocr.calls) >= 1
    assert parser.received[-1] == "scanned page words bread 10"
    assert parsed.order.metadata.input_type == "pdf_scanned"
    assert parsed.ai_response["attachment"]["ocr"]["pages"][0]["page"] == 1


def test_scanned_pdf_ocr_failure_flagged(store):
    scanned = make_pdf(with_text=False)
    ocr = FakeOCR(error=GoogleVisionError("rate limited", status=429, retryable=True))
    processor = PDFProcessor(
        text_parser=FakeTextParser(), vision_parser=FakeVisionParser(), ocr_service=ocr, attachment_store=store
    )
    parsed = processor.process(scanned)
    assert parsed.ai_response["ocr_failed"] is True
    assert parsed.ai_response["error_code"] == "OCR_UNAVAILABLE"


def test_scanned_pdf_legacy_vision_when_disabled(store):
    # GPT-vision fallback removed: disabled OCR now flags for review.
    scanned = make_pdf(with_text=False)
    legacy = FakeVisionParser()
    processor = PDFProcessor(
        text_parser=FakeTextParser(), vision_parser=legacy, ocr_service=FakeOCR(enabled=False), attachment_store=store
    )
    parsed = processor.process(scanned)
    assert not legacy.images_seen
    assert parsed.ai_response["ocr_failed"] is True
    assert parsed.ai_response["error_code"] == "OCR_UNAVAILABLE"


def test_multipage_scan_combines_pages(store):
    scanned = make_pdf(page_count=2, with_text=False)
    seen_texts: list[str] = []

    class PageOCR(FakeOCR):
        calls_by_page = ["page one bread 5", "page two milk 2"]

        def extract_text(self, image_bytes, filename="", mime_type="image/png"):
            idx = min(len(self.calls), 1)
            self.calls.append(image_bytes)
            from order_parser.ai.vision.ocr_service import OCRResult

            text = self.calls_by_page[idx]
            seen_texts.append(text)
            return OCRResult(text=text, metadata={"provider": "google_vision", "pages": 1})

    parser = FakeTextParser()
    processor = PDFProcessor(
        text_parser=parser, vision_parser=FakeVisionParser(), ocr_service=PageOCR(enabled=True), attachment_store=store
    )
    parsed = processor.process(scanned)
    joined = parser.received[-1]
    assert "page one bread 5" in joined and "page two milk 2" in joined
