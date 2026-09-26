from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Any


def ensure_directory(path: str | os.PathLike[str]) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def safe_json_loads(raw: str | None) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def normalize_extension(filename: str | None) -> str:
    if not filename:
        return ""
    return os.path.splitext(filename)[1].lower()


def guess_mime_type(filename: str | None) -> str:
    return mimetypes.guess_type(filename or "")[0] or "application/octet-stream"
