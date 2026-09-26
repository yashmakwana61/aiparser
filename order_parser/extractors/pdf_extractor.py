from __future__ import annotations

import re

import fitz


class PDFExtractor:
    @staticmethod
    def page_count(data: bytes) -> int:
        """Number of pages in the PDF (0 when it cannot be opened)."""
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception:
            return 0
        try:
            return doc.page_count
        finally:
            doc.close()

    @staticmethod
    def extract_text(data: bytes) -> str:
        """Extract all text from a PDF given as bytes."""
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            return "\n".join(page.get_text("text") for page in doc).strip()
        finally:
            doc.close()

    @staticmethod
    def render_pages(data: bytes, max_pages: int = 10, dpi: int = 150) -> list[bytes]:
        """Render PDF pages to PNG bytes (used as vision fallback for scans)."""
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            images: list[bytes] = []
            for page in doc:
                pixmap = page.get_pixmap(dpi=dpi)
                images.append(pixmap.tobytes("png"))
                if len(images) >= max_pages:
                    break
            return images
        finally:
            doc.close()

    @staticmethod
    def extract_tables(data: bytes) -> list[list[str]]:
        """Return each page's non-empty lines as a pseudo-table."""
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            tables: list[list[str]] = []
            for page in doc:
                text = page.get_text("text")
                rows = [row.strip() for row in re.split(r"\n+", text) if row.strip()]
                if rows:
                    tables.append(rows)
            return tables
        finally:
            doc.close()
