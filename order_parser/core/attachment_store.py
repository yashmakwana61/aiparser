from __future__ import annotations

import hashlib
from pathlib import Path

from order_parser.config import get_settings
from order_parser.utils import ensure_directory


class AttachmentStore:
    """Persists original attachments before processing (never discarded).

    Files are stored content-addressed under ``<log_dir>/uploads/`` and the
    SHA-256 hash plus path are returned so every downstream artifact can
    reference the exact source bytes.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        configured = directory or get_settings().upload_dir
        self.directory = Path(configured) if configured else Path(get_settings().log_dir) / "uploads"
        ensure_directory(self.directory)

    def save(self, data: bytes, suffix: str = ".bin") -> dict[str, object]:
        digest = hashlib.sha256(data).hexdigest()
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        safe_suffix = "".join(ch for ch in suffix if ch.isalnum() or ch == ".")[:16] or ".bin"
        path = self.directory / f"{digest[:32]}{safe_suffix}"
        if not path.exists():
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return {
            "path": str(path),
            "sha256": digest,
            "size_bytes": len(data),
        }
