from order_parser.processors.detector import InputType, detect_input_type


def test_detect_image_by_mime():
    assert detect_input_type("image/jpeg", "photo.jpg") == InputType.IMAGE


def test_detect_image_by_extension():
    assert detect_input_type("application/octet-stream", "scan.png") == InputType.IMAGE


def test_detect_pdf():
    assert detect_input_type("application/pdf", "order.pdf") == InputType.PDF


def test_detect_excel():
    assert detect_input_type(None, "order.xlsx") == InputType.EXCEL
    assert detect_input_type("application/vnd.ms-excel", "order.xls") == InputType.EXCEL


def test_detect_text_fallback():
    assert detect_input_type("text/plain", "message.txt") == InputType.TEXT
    assert detect_input_type(None, None) == InputType.TEXT