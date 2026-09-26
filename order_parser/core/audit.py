from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from order_parser.config import get_settings
from order_parser.core import metrics
from order_parser.utils import ensure_directory

logger = structlog.get_logger(__name__)

_chain_lock = threading.Lock()
_chain_cache: dict[Path, str] = {}


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _entry_digest(entry: dict) -> str:
    payload = {key: value for key, value in entry.items() if key != "hash"}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _read_last_hash(path: Path) -> str:
    """Recover the chain position from the file tail (restart-safe)."""
    try:
        if not path.exists():
            return ""
        last_line = ""
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last_line = line
        if not last_line:
            return ""
        record = json.loads(last_line)
        return str(record.get("hash", ""))
    except (OSError, json.JSONDecodeError):
        logger.warning("audit.chain_unrecoverable_starting_fresh", path=str(path))
        return ""


def _prev_hash(path: Path) -> str:
    with _chain_lock:
        if path not in _chain_cache:
            _chain_cache[path] = _read_last_hash(path)
        return _chain_cache[path]


def verify_audit_chain(path: str | Path) -> list[str]:
    """Return a list of integrity breaks; an empty list means intact.

    Legacy entries written before hash chaining have no ``hash`` field and are
    tolerated: they neither break nor extend the chain.
    """
    breaks: list[str] = []
    expected_prev = ""
    seen_hashed = False
    with open(path, "r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                breaks.append(f"line {line_number}: unparseable JSON")
                continue
            if "hash" not in entry:
                continue  # legacy entry
            if seen_hashed and entry.get("prev_hash") != expected_prev:
                breaks.append(f"line {line_number}: prev_hash mismatch")
            recomputed = _entry_digest(entry)
            if recomputed != entry.get("hash"):
                breaks.append(f"line {line_number}: content hash mismatch")
            expected_prev = str(entry.get("hash"))
            seen_hashed = True
    return breaks


def write_audit_entry(entry: dict, directory: str | Path | None = None) -> None:
    """Append a hash-chained audit record to <log_dir>/audit/YYYY-MM-DD.jsonl.

    Each entry stores the original message, input type, extracted text, AI
    response, normalized JSON, validation result, Odoo sales order reference
    and a timestamp - plus ``prev_hash``/``hash`` making the daily file
    tamper-evident. Write failures never propagate: observability must not
    take down order processing.
    """
    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    day = entry["timestamp"][:10]
    root = Path(directory) if directory else Path(get_settings().log_dir) / "audit"
    path = root / f"{day}.jsonl"
    try:
        ensure_directory(root)
        prev = _prev_hash(path)
        entry["prev_hash"] = prev
        entry["hash"] = _entry_digest(entry)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_canonical(entry) + "\n")
        with _chain_lock:
            _chain_cache[path] = entry["hash"]
    except Exception:
        metrics.incr("audit_failures_total")
        logger.exception("audit.entry_write_failed", order_id=entry.get("order_id"))
        return
    logger.info(
        "audit.entry_written",
        order_id=entry.get("order_id"),
        status=entry.get("status"),
    )


# ------------------------------------------------------------------ archive


def audit_day_files(directory: str | Path | None = None) -> list[Path]:
    """All daily audit files, oldest first."""
    root = Path(directory) if directory else Path(get_settings().log_dir) / "audit"
    if not root.exists():
        return []
    return sorted(root.glob("*.jsonl"))


def verify_audit_directory(directory: str | Path | None = None) -> dict:
    """Verify every daily chain; an intact archive reports integrity_ok=True.

    Chains are per-day by design, so each file is checked independently and
    a break in one day never masks or cascades into another.
    """
    breaks: dict[str, list[str]] = {}
    files_checked = 0
    for path in audit_day_files(directory):
        files_checked += 1
        issues = verify_audit_chain(path)
        if issues:
            breaks[path.name] = issues
    return {
        "integrity_ok": not breaks,
        "files_checked": files_checked,
        "breaks": breaks,
    }


def _day_from_name(path: Path):
    try:
        return datetime.strptime(path.stem, "%Y-%m-%d").date()
    except ValueError:
        return None


def prune_audit_days(
    retention_days: int,
    directory: str | Path | None = None,
    now: datetime | None = None,
) -> int:
    """Delete whole-day audit files strictly older than the window.

    Safe because chains are daily-independent: removing an old day cannot
    break any surviving file's hash chain. Files whose name is not a valid
    date are never touched. Returns the number of files deleted.
    """
    if retention_days <= 0:
        return 0
    cutoff_date = (now or datetime.now(timezone.utc)).date() - timedelta(days=int(retention_days))
    root = Path(directory) if directory else Path(get_settings().log_dir) / "audit"
    removed = 0
    for path in root.glob("*.jsonl"):
        day = _day_from_name(path)
        if day is None or day >= cutoff_date:
            continue
        try:
            path.unlink()
        except OSError:
            logger.exception("retention.audit_delete_failed", file=path.name)
            continue
        removed += 1
    metrics.incr("retention_audit_files_pruned_total", value=removed)
    return removed
