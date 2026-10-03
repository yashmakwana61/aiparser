"""AI extraction layers.

* ``text_parser``: semantic order normalization from already-extracted text
  via the Puter AI gateway (ChatGPT text model, e.g. gpt-4.1). Google Vision
  remains the OCR provider for images/scanned PDFs; the text parser never
  receives images.
* ``vision_parser``: DEPRECATED direct-vision parsing (Puter/GPT), kept for
  backward compatibility / rollback only — not part of the production path.
"""