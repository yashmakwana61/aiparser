import io

import pandas as pd

from order_parser.extractors.excel_extractor import ExcelExtractor


def test_detect_columns():
    df = pd.DataFrame({"Item": ["Keyboard"], "Qty": [2], "Rate": [100], "Units": ["Nos"]})
    mapping = ExcelExtractor.detect_columns(df)
    assert mapping["product_name"] == "Item"
    assert mapping["quantity"] == "Qty"
    assert mapping["unit_price"] == "Rate"
    assert mapping["uom"] == "Units"


def test_detect_columns_variants():
    df = pd.DataFrame({"Product Name": ["A"], "Qty Ordered": [1]})
    mapping = ExcelExtractor.detect_columns(df)
    assert mapping["product_name"] == "Product Name"
    assert mapping["quantity"] == "Qty Ordered"


def test_extract_items():
    df = pd.DataFrame({"Item": ["Keyboard", "Mouse"], "Qty": [2, 5], "Rate": [100, 50]})
    mapping = ExcelExtractor.detect_columns(df)
    items = ExcelExtractor.extract_items(df, mapping)
    assert len(items) == 2
    assert items[0] == {"product_name": "Keyboard", "quantity": 2.0, "unit_price": 100.0, "uom": "Units"}
    assert items[1]["quantity"] == 5.0


def test_extract_items_skips_empty_rows():
    df = pd.DataFrame({"Item": ["Keyboard", None, "Mouse"], "Qty": [2, 3, 5]})
    items = ExcelExtractor.extract_items(df, ExcelExtractor.detect_columns(df))
    assert len(items) == 2


def test_to_text_preview():
    df = pd.DataFrame({"Item": ["A"], "Qty": [1]})
    preview = ExcelExtractor.to_text_preview(df)
    assert "Item" in preview
    assert "A" in preview


def test_excel_processor_direct_parse():
    from order_parser.processors.excel_processor import ExcelProcessor

    df = pd.DataFrame({"Item": ["Keyboard"], "Qty": [2], "Rate": [100]})
    buffer = io.BytesIO()
    df.to_excel(buffer, index=False)
    parsed = ExcelProcessor().process(buffer.getvalue())
    assert len(parsed.order.items) == 1
    assert parsed.order.items[0].product_name == "Keyboard"
    assert parsed.order.items[0].quantity == 2.0
    assert parsed.order.metadata.confidence == 100.0