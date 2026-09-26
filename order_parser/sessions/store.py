from __future__ import annotations

import json
import os
import structlog
import threading
import uuid
from pathlib import Path

from order_parser.config import get_settings
from order_parser.core import metrics
from order_parser.sessions.models import ACTIVE_STATUSES, StaffSession
from order_parser.utils import ensure_directory

logger = structlog.get_logger(__name__)


class SessionStore:
    """Persistent JSON-file store for staff order sessions.

    One file per session under ``<log_dir>/sessions/<session_id>.json``
    (or ``session_store_dir`` when configured). Writes are atomic and
    serialized; the narrow interface keeps a PostgreSQL migration a
    drop-in replacement later.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        configured = directory or get_settings().session_store_dir
        self.directory = Path(configured) if configured else Path(get_settings().log_dir) / "sessions"
        ensure_directory(self.directory)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ paths

    def _path(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.json"

    def attachment_dir(self, session_id: str) -> Path:
        path = self.directory / "attachments" / session_id
        ensure_directory(path)
        return path

    # ------------------------------------------------------------------- CRUD

    def save(self, session: StaffSession) -> None:
        with self._lock:
            final = self._path(session.session_id)
            tmp = final.with_name(f".{final.stem}.{uuid.uuid4().hex}.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(session.model_dump(mode="json"), ensure_ascii=False, indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)

    def _load(self, path: Path) -> StaffSession | None:
        """Read one session file; corrupt files are quarantined for forensics."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return StaffSession.model_validate(data)
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("sessions.store_unreadable", path=str(path))
            return None
        except (json.JSONDecodeError, ValueError):
            self._quarantine(path)
            return None

    def _quarantine(self, path: Path) -> None:
        metrics.incr("session_files_corrupt_total")
        target = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex[:8]}")
        try:
            path.rename(target)
            logger.warning("sessions.corrupt_file_quarantined", file=target.name)
        except OSError:
            logger.exception("sessions.quarantine_failed", path=str(path))

    def get(self, session_id: str) -> StaffSession | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        return self._load(path)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            path = self._path(session_id)
            if path.exists():
                path.unlink()
                return True
            return False

    # ------------------------------------------------------------------ queries

    def list_all(self) -> list[StaffSession]:
        sessions: list[StaffSession] = []
        for path in self.directory.glob("*.json"):
            session = self._load(path)
            if session is not None:
                sessions.append(session)
        return sessions

    def list_active(self) -> list[StaffSession]:
        return [s for s in self.list_all() if s.status in ACTIVE_STATUSES]

    def list_by_staff(self, staff_id: str) -> list[StaffSession]:
        return [s for s in self.list_all() if s.staff_id == staff_id]
