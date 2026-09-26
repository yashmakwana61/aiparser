from __future__ import annotations

import json
import uuid
from pathlib import Path

import structlog

from order_parser.config import get_settings
from order_parser.core import metrics
from order_parser.utils import ensure_directory

logger = structlog.get_logger(__name__)


class PendingStore:
    """File-based store for orders awaiting confirmation or manual review.

    Records live under ``logs/pending/<order_id>.json``. Listings are
    deterministic: newest ``created_at`` first (oldest work never silently
    sinks to the bottom of the queue). Corrupt files are quarantined for
    forensics instead of being silently skipped forever.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory else Path(get_settings().log_dir) / "pending"
        ensure_directory(self.directory)

    def _path(self, order_id: str) -> Path:
        return self.directory / f"{order_id}.json"

    def save(self, record: dict) -> None:
        self._path(record["order_id"]).write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    def get(self, order_id: str) -> dict | None:
        path = self._path(order_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def delete(self, order_id: str) -> None:
        path = self._path(order_id)
        if path.exists():
            path.unlink()

    def _quarantine(self, path: Path) -> None:
        metrics.incr("pending_files_corrupt_total")
        target = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex[:8]}")
        try:
            path.rename(target)
            logger.warning("pending.corrupt_file_quarantined", file=target.name)
        except OSError:
            logger.exception("pending.quarantine_failed", path=str(path))

    def _load(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("pending.store_unreadable", path=str(path))
            return None
        except (json.JSONDecodeError, ValueError):
            self._quarantine(path)
            return None

    @staticmethod
    def _sort_key(record: dict) -> tuple[str, str]:
        # ISO-8601 timestamps sort correctly as strings; records without one
        # sort oldest so they cannot hide at the top of the queue.
        return (str(record.get("created_at") or ""), str(record.get("order_id") or ""))

    def list(
        self,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """Filtered, newest-first listing with optional windowing.

        ``limit=None`` keeps the legacy behavior of returning every match;
        pagination is applied after filtering and sorting.
        """
        records: list[dict] = []
        for path in self.directory.glob("*.json"):
            record = self._load(path)
            if record is None:
                continue
            if status and record.get("status") != status:
                continue
            records.append(record)
        records.sort(key=self._sort_key, reverse=True)
        start = max(0, int(offset or 0))
        if limit is None:
            return records[start:]
        end = start + max(0, int(limit))
        return records[start:end]

    def count(self, status: str | None = None) -> int:
        return len(self.list(status=status))
