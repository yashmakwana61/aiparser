from __future__ import annotations

from typing import Any

from order_parser.ai.text_parser import TextParser
from order_parser.models import ParsedOrder
from order_parser.normalizers.order_normalizer import OrderNormalizer


class TextProcessor:
    def __init__(self, parser: TextParser | None = None):
        self.parser = parser or TextParser()

    def process(self, content: str) -> ParsedOrder:
        ai_response = self.parser.parse(content)
        order = OrderNormalizer.normalize(ai_response, source="", input_type="text")
        return ParsedOrder(order=order, ai_response=ai_response, extracted_text=content)
