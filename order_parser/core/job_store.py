from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any

import structlog

from order_parser.config import get_settings
from order_parser.core.job import JobRecord, JobStatus
from order_parser.utils import ensure_directory

logger = structlog.get_logger(__name__)

_JOB_ID_RE = re.compile(r"^ORD-(\d{8})-(\d+)$")


class JobStore:
    """File-backed job registry. Each job is a JSON file under <log_dir>/jobs/<job_id>.json

    Thread-safe, crash-safe via atomic write. Supports pagination and status filtering.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        configured = getattr(get_settings(), "job_store_dir", "") if hasattr(get_settings(), "job_store_dir") else ""
        if directory is not None:
            self.directory = Path(directory)
        elif configured:
            self.directory = Path(configured)
        else:
            self.directory = Path(get_settings().log_dir) / "jobs"
        ensure_directory(self.directory)
        self._lock = threading.Lock()

    def _path(self, job_id: str) -> Path:
        safe = "".join(ch for ch in job_id if ch.isalnum() or ch in ("-", "_"))
        return self.directory / f"{safe}.json"

    def next_job_id(self) -> str:
        """Allocate the next ORD-YYYYMMDD-###### id, durable across restarts.

        The per-day counter lives in ``.sequence-YYYYMMDD`` next to the job
        files (atomic tmp+replace write under the store lock). When the
        counter file is missing or corrupt, the sequence is re-seeded by
        scanning existing job files — so a restart, redeploy or lost counter
        can never reuse an id and overwrite an earlier job.
        """
        from datetime import datetime, timezone

        date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
        with self._lock:
            seq = self._read_sequence(date_part)
            if seq is None:
                seq = self._scan_max_sequence(date_part)
                logger.warning("job_store.sequence_reseeded", date=date_part, seq=seq)
            seq += 1
            self._write_sequence(date_part, seq)
            return f"ORD-{date_part}-{seq:06d}"

    def _sequence_path(self, date_part: str) -> Path:
        return self.directory / f".sequence-{date_part}"

    def _read_sequence(self, date_part: str) -> int | None:
        try:
            value = int(self._sequence_path(date_part).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return value if value >= 0 else None

    def _scan_max_sequence(self, date_part: str) -> int:
        best = 0
        for path in self.directory.glob(f"ORD-{date_part}-*.json"):
            match = _JOB_ID_RE.match(path.stem)
            if match is None or match.group(1) != date_part:
                continue
            try:
                best = max(best, int(match.group(2)))
            except ValueError:
                continue
        return best

    def _write_sequence(self, date_part: str, seq: int) -> None:
        path = self._sequence_path(date_part)
        tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:6]}")
        tmp.write_text(str(seq), encoding="utf-8")
        tmp.replace(path)

    def save(self, job: JobRecord) -> None:
        data = job.model_dump()
        path = self._path(job.job_id)
        tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:6]}")
        with self._lock:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            tmp.replace(path)

    def get(self, job_id: str) -> JobRecord | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return JobRecord.model_validate(raw)
        except Exception:
            logger.exception("job_store.get_failed", job_id=job_id)
            return None

    def get_raw(self, job_id: str) -> dict[str, Any] | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def update(self, job_id: str, **fields: Any) -> JobRecord | None:
        job = self.get(job_id)
        if not job:
            return None
        for k, v in fields.items():
            if hasattr(job, k):
                setattr(job, k, v)
        job.updated_at = _now_iso()
        self.save(job)
        return job

    def list(
        self,
        status: JobStatus | str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JobRecord]:
        records: list[JobRecord] = []
        for path in self.directory.glob("*.json"):
            if path.name.startswith("."):
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                rec = JobRecord.model_validate(raw)
            except Exception:
                continue
            if status and rec.status != status and str(rec.status) != str(status):
                continue
            records.append(rec)
        # newest first by created_at
        records.sort(key=lambda r: (r.created_at, r.job_id), reverse=True)
        start = max(0, int(offset or 0))
        if limit is None:
            return records[start:]
        return records[start : start + max(0, int(limit))]

    def count(self, status: JobStatus | str | None = None) -> int:
        return len(self.list(status=status))

    def delete(self, job_id: str) -> bool:
        path = self._path(job_id)
        if path.exists():
            try:
                path.unlink()
                return True
            except OSError:
                return False
        return False

    def find_by_hash(self, input_hash: str, window_hours: int = 24) -> JobRecord | None:
        if not input_hash:
            return None
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        best: JobRecord | None = None
        for rec in self.list():
            if rec.input_hash != input_hash:
                continue
            try:
                created = datetime.fromisoformat(rec.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < cutoff:
                    continue
            except Exception:
                continue
            if best is None or rec.created_at > best.created_at:
                best = rec
        return best


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
