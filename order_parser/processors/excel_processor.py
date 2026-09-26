from __future__ import annotations

import structlog

from order_parser.ai.text_parser import TextParser
from order_parser.extractors.excel_extractor import ExcelExtractor
from order_parser.models import ParsedOrder
from order_parser.normalizers.order_normalizer import OrderNormalizer

logger = structlog.get_logger(__name__)


class ExcelProcessor:
    """Structured Excel extraction layer (Phase 4).

    Every sheet is inspected; the header row is detected (never assumed to
    be row 1) and items are aggregated from all mappable sheets. Customer
    hints above the tables are captured. Blank rows, extra columns, merged
    cells and uneven rows are tolerated. When the structure is ambiguous the
    candidate data is serialized as a preview and sent to AI interpretation -
    nothing is invented.
    """

    def __init__(self, parser: TextParser | None = None):
        self.parser = parser or TextParser()

    def process(self, data: bytes, filename: str = "order.xlsx") -> ParsedOrder:
        try:
            items, info = ExcelExtractor.extract_from_workbook(data)
            if items:
                ai_response = {
                    "customer": {"name": info.get("customer_hint", "")},
                    "items": items,
                    "notes": "",
                    "confidence": 100.0,
                    "excel": info,
                }
                order = OrderNormalizer.normalize(ai_response, source="", input_type="excel")
                return ParsedOrder(order=order, ai_response=ai_response)
        except Exception:
            logger.exception("excel.parse_failed", filename=filename)

        preview = ExcelExtractor.extract_preview_all_sheets(data)
        if not preview:
            preview = f"(Excel file {filename} could not be read)"
        ai_response = self.parser.parse(preview)
        ai_response["excel"] = {"fallback": "ai_interpretation"}
        order = OrderNormalizer.normalize(ai_response, source="", input_type="excel")
        return ParsedOrder(order=order, ai_response=ai_response, extracted_text=preview)
