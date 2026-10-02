"""AI extraction layers.

* ``text_parser``: semantic order extraction from already-extracted text via
  the locally hosted NuExtract model (Ollama). Google Vision remains the OCR
  provider; NuExtract never receives images.
* ``vision_parser``: DEPRECATED direct-vision parsing (Puter/GPT), kept for
  backward compatibility / rollback only — not part of the production path.
"""