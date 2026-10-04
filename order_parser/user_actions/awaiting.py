"""Tiny file-backed store for pending free-text correction inputs.

When the bot asks "type the exact product name", the next text message from
that user is routed to the correction service instead of becoming a new
order. Entries expire quickly; any unrelated message aborts the step.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from order_parser.config import get_settings
from order_parser.utils import ensure_directory


class AwaitingStore:
    """Maps telegram user id -> {case_id, verb, item_index, expires_at}."""

    def __init__(self, directory: str | Path | None = None, ttl_seconds: int = 900) -> None:
        base = Path(directory) if directory else Path(get_settings().log_dir)
        ensure_directory(base)
        self.path = base / "awaiting_inputs.json"
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_name(f".awaiting.tmp-{uuid.uuid4().hex[:6]}")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def set(self, user_key: str, case_id: str, verb: str, item_index: int | None = None) -> None:
        with self._lock:
            data = self._prune_locked()
            data[str(user_key)] = {
                "case_id": case_id, "verb": verb, "item_index": item_index,
                "expires_at": time.time() + self.ttl_seconds,
            }
            self._save(data)

    def pop(self, user_key: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._prune_locked()
            entry = data.pop(str(user_key), None)
            self._save(data)
            return entry if isinstance(entry, dict) else None

    def peek(self, user_key: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._prune_locked()
            entry = data.get(str(user_key))
            return dict(entry) if isinstance(entry, dict) else None

    def clear(self, user_key: str) -> None:
        with self._lock:
            data = self._prune_locked()
            data.pop(str(user_key), None)
            self._save(data)

    def _prune_locked(self) -> dict[str, Any]:
        now = time.time()
        data = self._load()
        return {k: v for k, v in data.items()
                if isinstance(v, dict) and float(v.get("expires_at") or 0) > now}
