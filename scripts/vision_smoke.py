"""Manual smoke test: file -> Google Vision OCR -> Puter/ChatGPT normalization.

Usage:
    python scripts/vision_smoke.py <image-or-pdf-path>

No Telegram, email, or Odoo involved. Prints each hop's output and timing.
Exit codes: 0 = full chain OK, 1 = a hop failed, 2 = usage/config error.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def status_report() -> bool:
    from order_parser.config import get_settings

    s = get_settings()
    print("== configuration ==")
    print(f"  google vision enabled : {s.enable_google_vision}")
    print(f"  google vision api key : {'set' if s.google_vision_api_key else 'MISSING'}")
    print(f"  puter token           : {'set' if s.puter_auth_token else 'MISSING'}")
    print(f"  text model            : {s.ai_text_model}")
    ok = (
        s.enable_google_vision
        and bool(s.google_vision_api_key)
        and bool(s.puter_auth_token)
        and bool(s.ai_text_model)
    )
    if not ok:
        print("\nFix the MISSING/false entries in .env first.")
    return ok


def ocr_file(path: Path) -> tuple[str, float]:
    """Return (raw text, seconds) extracting via text layer or Vision OCR."""
    from order_parser.config import get_settings
    from order_parser.ai.vision.ocr_service import OCRError, VisionOCRService

    started = time.monotonic()
    suffix = path.suffix.lower()

    if suffix in IMAGE_MIMES:
        svc = VisionOCRService()
        if not svc.enabled:
            raise RuntimeError("Google Vision not configured (ENABLE_GOOGLE_VISION / API key)")
        result = svc.extract_text(path.read_bytes(), filename=path.name, mime_type=IMAGE_MIMES[suffix])
        return result.text, time.monotonic() - started

    if suffix == ".pdf":
        import fitz

        settings = get_settings()
        doc = fitz.open(str(path))
        text_layer = "\n".join(page.get_text() for page in doc).strip()
        min_chars = settings.pdf_min_chars_per_page * max(1, doc.page_count)
        if len(text_layer) >= min_chars:
            print(f"  (digital PDF: {len(text_layer)} chars of text layer, no OCR needed)")
            return text_layer, time.monotonic() - started

        svc = VisionOCRService()
        if not svc.enabled:
            raise RuntimeError("Scanned PDF needs Google Vision, which is not configured")
        chunks: list[str] = []
        for page in doc:
            pix = page.get_pixmap(dpi=150)
            png = pix.tobytes("png")
            result = svc.extract_text(png, filename=f"{path.name}#p{page.number + 1}")
            chunks.append(result.text)
        return "\n".join(chunks), time.monotonic() - started

    raise RuntimeError(f"Unsupported file type: {suffix}")


def normalize(raw_text: str) -> tuple[dict, float]:
    from order_parser.ai.text_parser import TextParser

    started = time.monotonic()
    parsed = TextParser().parse(raw_text)
    return parsed, time.monotonic() - started


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = Path(argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        return 2

    if not status_report():
        return 2

    print(f"\n== hop 1: extract text from {path.name} ==")
    try:
        raw_text, ocr_secs = ocr_file(path)
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return 1
    preview = raw_text.strip()[:400] or "(empty)"
    more = "..." if len(raw_text.strip()) > 400 else ""
    print(f"  [{ocr_secs:.1f}s] raw text ({len(raw_text)} chars):\n  {preview}{more}")

    if not raw_text.strip():
        print("  FAILED: no text extracted — try a clearer/sharper file.")
        return 1

    print("\n== hop 2: normalize with ChatGPT via Puter ==")
    try:
        parsed, ai_secs = normalize(raw_text)
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"  [{ai_secs:.1f}s] normalized JSON:")
    print(json.dumps(parsed, indent=2, ensure_ascii=False, default=str))

    print(f"\n== done: OCR {ocr_secs:.1f}s + AI {ai_secs:.1f}s ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
