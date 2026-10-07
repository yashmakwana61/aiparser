"""AI quota-exhausted mapping: billing refusals get their own user message."""

from order_parser.ai.text_parser import classify_ai_failure


def _err(message):
    return RuntimeError(message)


def test_quota_markers_map_to_quota_code():
    assert classify_ai_failure(_err("Puter AI request failed (HTTP 402): "
                                    '{"error":"No usage left for request."}')) == "AI_QUOTA_EXHAUSTED"
    assert classify_ai_failure(_err("HTTP 402 insufficient_funds")) == "AI_QUOTA_EXHAUSTED"
    assert classify_ai_failure(_err("quota exceeded for project")) == "AI_QUOTA_EXHAUSTED"
    assert classify_ai_failure(_err("402 Payment Required: billing balance empty")) == "AI_QUOTA_EXHAUSTED"


def test_non_quota_failures_stay_generic():
    assert classify_ai_failure(_err("AI response was not valid JSON")) == "AI_INTERPRETATION_FAILED"
    assert classify_ai_failure(_err("HTTP 500 gateway timeout")) == "AI_INTERPRETATION_FAILED"
    assert classify_ai_failure(_err("connection reset by peer")) == "AI_INTERPRETATION_FAILED"
    # A bare number that merely appears in order-adjacent text is not a refusal.
    assert classify_ai_failure(_err("model returned 402 words")) == "AI_INTERPRETATION_FAILED"


def test_quota_code_has_upload_action_with_topup_guidance():
    from order_parser.user_actions.resolver import build_actions

    actions = build_actions("ORD-1", {}, {}, {},
                            {"ai_response": {"ocr_failed": True, "error_code": "AI_QUOTA_EXHAUSTED"}})
    assert len(actions) == 1
    problem = actions[0].problem
    assert problem.code == "AI_QUOTA_EXHAUSTED"
    assert "credit" in problem.description.lower() or "balance" in problem.description.lower()
    assert actions[0].solution.kind == "upload"
    assert "Traceback" not in problem.description


def test_image_processor_flags_quota_code(tmp_path):
    from types import SimpleNamespace

    from order_parser.ai.vision.ocr_service import OCRResult
    from order_parser.core.attachment_store import AttachmentStore
    from order_parser.processors.image_processor import ImageProcessor

    class BoomParser:
        def parse(self, text):
            raise RuntimeError('Puter AI request failed (HTTP 402): {"error":"No usage left"}')

    ocr = SimpleNamespace(
        enabled=True,
        extract_text=lambda *a, **k: OCRResult(text="some order text", metadata={}))
    processor = ImageProcessor(text_parser=BoomParser(), ocr_service=ocr,
                               attachment_store=AttachmentStore(tmp_path / "att"))
    parsed = processor.process(b"\x89PNG\r\n\x1a\n" + b"fake-bytes", filename="o.png")
    assert parsed.ai_response["ocr_failed"] is True
    assert parsed.ai_response["error_code"] == "AI_QUOTA_EXHAUSTED"
    assert parsed.order.customer.name == ""
