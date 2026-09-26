"""Google Vision OCR adapter (Phase 4).

OCR is responsible ONLY for extracting raw text. Semantic interpretation of
the order remains with the AI layer. Failures never create orders - they are
flagged so the pipeline routes them to review.
"""
