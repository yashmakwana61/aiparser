from __future__ import annotations

from enum import Enum


class InputType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    PDF = "pdf"
    EXCEL = "excel"


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic"}
EXCEL_MIME_TYPES = {
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def detect_input_type(mime_type: str | None = None, filename: str | None = None) -> InputType:
    """Input detection layer: classify incoming bytes by MIME type and filename."""
    mime = (mime_type or "").lower()
    name = (filename or "").lower()

    if mime.startswith("image/") or any(name.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return InputType.IMAGE
    if mime == "application/pdf" or name.endswith(".pdf"):
        return InputType.PDF
    if name.endswith((".xls", ".xlsx", ".xlsm")) or mime in EXCEL_MIME_TYPES:
        return InputType.EXCEL
    return InputType.TEXT
