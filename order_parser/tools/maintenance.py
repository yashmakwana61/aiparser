"""Maintenance CLI for operational runbook procedures.

Usage: python -m order_parser.tools.maintenance <command> [options]

Every command prints a machine-readable JSON summary on stdout and exits
0 on success, 1 on verification failure, 2 on usage errors. Directory
arguments default to the configured settings (log_dir layout).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from order_parser.core.audit import verify_audit_directory
from order_parser.core.pending_store import PendingStore
from order_parser.core.retention import prune_sessions


def _settings():
    from order_parser.config import get_settings

    return get_settings()


def cmd_verify_audit(args) -> dict:
    return verify_audit_directory(args.dir)


def cmd_list_quarantine(args) -> dict:
    root = Path(args.dir) if args.dir else Path(_settings().log_dir)
    files = sorted(root.rglob("*.corrupt-*")) if root.exists() else []
    entries = [{"path": str(f), "bytes": f.stat().st_size} for f in files]
    return {"count": len(entries), "total_bytes": sum(e["bytes"] for e in entries), "files": entries}


def cmd_pending_summary(args) -> dict:
    store = PendingStore(args.dir)
    orders = store.list()
    by_status = Counter(order.get("status", "unknown") for order in orders)
    return {"count": len(orders), "by_status": dict(by_status)}


def cmd_prune_sessions(args) -> dict:
    from datetime import datetime

    from order_parser.core.retention import collect_prunable_sessions
    from order_parser.sessions.store import SessionStore

    store = SessionStore(args.store_dir)
    now = datetime.fromisoformat(args.now) if args.now else None

    if args.dry_run:
        candidates = collect_prunable_sessions(store, retention_days=args.days, now=now)
        return {
            "dry_run": True,
            "sessions": [
                {
                    "session_id": c.session_id,
                    "status": c.status,
                    "updated_at": c.updated_at,
                    "bytes": c.bytes,
                }
                for c in candidates
            ],
            "session_count": len(candidates),
            "total_bytes": sum(c.bytes for c in candidates),
        }

    result = prune_sessions(store, retention_days=args.days, now=now)
    return {
        "sessions_pruned": result.sessions_pruned,
        "corrupt_files_pruned": result.corrupt_files_pruned,
        "bytes_freed": result.bytes_freed,
        "audit_files_pruned": result.audit_files_pruned,
    }


def cmd_export_pending(args) -> dict:
    import time

    store = PendingStore(args.dir)
    orders = store.list(status=args.status)

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        out_path = store.directory.parent / f"pending-export-{stamp}.jsonl"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for record in orders:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    return {"count": len(orders), "path": str(out_path), "bytes": out_path.stat().st_size}


def cmd_mine_corrections(args) -> dict:
    """Aggregate user corrections from the audit archive into alias candidates.

    Repeated identical original->target corrections (default: 2+) are strong
    alias candidates; corrections without usable targets are reported as
    data gaps. Read-only; creating aliases stays an explicit human action.
    """
    from order_parser.core.audit import audit_day_files

    root = Path(args.dir) if args.dir else None
    files = audit_day_files(root) if root else audit_day_files()
    if args.dir and root is not None and root.is_file():
        files = [root]
    counter: Counter = Counter()
    gaps = 0
    total = 0
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("event") != "user_action":
                continue
            if entry.get("action") != "correction_applied":
                continue
            total += 1
            correction = (entry.get("detail") or {}).get("correction") or {}
            field = str(correction.get("field") or "")
            original = str(correction.get("original_value") or "").strip()
            target = correction.get("target") or {}
            target_id = target.get("product_id", target.get("partner_id"))
            if field and original and target_id is not None:
                counter[(field, original, str(target_id))] += 1
            else:
                gaps += 1
    suggestions = [
        {"field": field, "original_value": original, "target_id": target_id, "times": count}
        for (field, original, target_id), count in counter.most_common()
        if count >= max(1, int(args.min_repeats))
    ]
    return {"corrections_seen": total, "without_usable_target": gaps,
            "alias_candidates": suggestions}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maintenance", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("verify-audit", help="Verify hash chains of the audit archive")
    p.add_argument("--dir", default=None, help="Audit directory (default <log_dir>/audit)")
    p.set_defaults(func=cmd_verify_audit)

    p = sub.add_parser("list-quarantine", help="List quarantined corrupt files")
    p.add_argument("--dir", default=None, help="Directory to scan (default log_dir)")
    p.set_defaults(func=cmd_list_quarantine)

    p = sub.add_parser("pending-summary", help="Summarize review queue by status")
    p.add_argument("--dir", default=None, help="Pending directory (default <log_dir>/pending)")
    p.set_defaults(func=cmd_pending_summary)

    p = sub.add_parser("prune-sessions", help="Prune terminal sessions older than --days")
    p.add_argument("--days", type=int, required=True)
    p.add_argument("--store-dir", default=None, help="Session store (default from settings)")
    p.add_argument("--dry-run", action="store_true", help="List what would be deleted; delete nothing")
    p.add_argument("--now", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_prune_sessions)

    p = sub.add_parser("export-pending", help="Export review-queue records to a JSONL snapshot")
    p.add_argument("--dir", default=None, help="Pending directory (default <log_dir>/pending)")
    p.add_argument("--status", default=None, help="Only export this status (e.g. review)")
    p.add_argument("--out", default=None, help="Output .jsonl path (default pending-export-<ts>.jsonl next to the store)")
    p.set_defaults(func=cmd_export_pending)

    p = sub.add_parser("mine-corrections", help="Suggest aliases from repeated user corrections")
    p.add_argument("--dir", default=None, help="Audit day file or directory (default <log_dir>/audit)")
    p.add_argument("--min-repeats", type=int, default=2, help="Minimum repeats to suggest (default 2)")
    p.set_defaults(func=cmd_mine_corrections)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    result = args.func(args)
    print(json.dumps(result, indent=2))
    return 0 if args.command != "verify-audit" else (0 if result["integrity_ok"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
