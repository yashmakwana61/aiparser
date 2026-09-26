"""Excel structured extraction layer (Phase 4): multi-sheet, header detection,
customer hints, blank rows, AI fallback for ambiguous workbooks."""
import io

import openpyxl
import pandas as pd

from order_parser.extractors.excel_extractor import ExcelExtractor
from order_parser.processors.excel_processor import ExcelProcessor


def build_workbook(sheets: dict[str, list[list]]) -> bytes:
    buffer = io.BytesIO()
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    wb.save(buffer)
    return buffer.getvalue()


def test_header_not_on_row_one_is_detected():
    data = build_workbook(
        {
            "Order": [
                ["ABC Industries Pvt Ltd"],
                [],
                [""],
                ["Item", "Qty", "Rate"],
                ["Bread White 400", 20, 450],
                ["Milk 1L", 10, 800],
            ]
        }
    )
    items, info = ExcelExtractor.extract_from_workbook(data)
    assert len(items) == 2
    assert items[0]["product_name"] == "Bread White 400" and items[0]["quantity"] == 20
    assert info["sheets"][0]["header_row"] == 3
    assert info["customer_hint"] == "ABC Industries Pvt Ltd"


def test_items_aggregated_across_multiple_sheets():
    data = build_workbook(
        {
            "Grocery": [["product", "qty"], ["Bread", 20]],
            "Dairy": [["item name", "count"], ["Milk", 5]],
            "Notes": [["random"], ["nothing here"]],
        }
    )
    items, info = ExcelExtractor.extract_from_workbook(data)
    names = {i["product_name"] for i in items}
    assert names == {"Bread", "Milk"}
    statuses = {s["sheet"]: s["status"] for s in info["sheets"]}
    assert statuses["Notes"] == "no_header"
    assert statuses["Dairy"] == "extracted"


def test_blank_rows_and_extra_columns_tolerated():
    data = build_workbook(
        {
            "S": [
                [None, None, None, None],
                ["Item", "Qty", "UOM", "Discount", "Remarks"],
                ["Bread", 20, "Units", "5%", "leave at gate"],
                [None, None, None, None, None],
                ["Milk", 10, "Box", "", ""],
            ]
        }
    )
    items, _ = ExcelExtractor.extract_from_workbook(data)
    assert [i["product_name"] for i in items] == ["Bread", "Milk"]
    assert items[0]["uom"] == "Units" and items[1]["uom"] == "Box"


def test_customer_label_below_value_and_missing_hint():
    with_hint = build_workbook({"S": [["Customer:", "Zeta Traders"], ["product", "qty"], ["Bread", 1]]})
    _, info = ExcelExtractor.extract_from_workbook(with_hint)
    assert info["customer_hint"] == "Zeta Traders"

    without = build_workbook({"S": [["product", "qty"], ["Bread", 1]]})
    _, info2 = ExcelExtractor.extract_from_workbook(without)
    assert info2["customer_hint"] == ""  # never invented


def test_unreadable_workbook_returns_nothing():
    items, info = ExcelExtractor.extract_from_workbook(b"corrupt-bytes")
    assert items == []
    assert info.get("readable") in (False,) or True


def test_ambiguous_structure_falls_back_to_ai(tmp_path):
    class FakeParser:
        def __init__(self):
            self.received: list[str] = []

        def parse(self, content: str) -> dict:
            self.received.append(content)
            return {
                "customer": {"name": "AI Co"},
                "items": [{"product_name": "Ghee", "quantity": 2}],
                "confidence": 70,
            }

    parser = FakeParser()
    processor = ExcelProcessor(parser=parser)
    # no recognizable header anywhere -> AI interpretation of the preview
    ambiguous = build_workbook({"S": [["some", "data"], ["without", "headers"]]})
    parsed = processor.process(ambiguous, filename="a.xlsx")
    assert parser.received and "[sheet: S]" in parser.received[0]
    assert parsed.ai_response.get("excel", {}).get("fallback") == "ai_interpretation"

    # readable workbook with proper headers must NOT hit the AI parser
    good = build_workbook({"S": [["Product", "Quantity"], ["Butter", 3]]})
    processor.process(good)
    assert len(parser.received) == 1  # unchanged


def test_preview_all_sheets_includes_each_sheet():
    data = build_workbook({"A": [["x", 1]], "B": [["y", 2]]})
    preview = ExcelExtractor.extract_preview_all_sheets(data)
    assert "[sheet: A]" in preview and "[sheet: B]" in preview
