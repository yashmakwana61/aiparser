from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from order_parser.config import get_settings
from order_parser.resolution.normalization import normalize_name
from order_parser.utils import ensure_directory

logger = structlog.get_logger(__name__)


class AliasConflictError(ValueError):
    """Raised when creating an alias that already maps to a different target."""


class AliasRecord(BaseModel):
    id: str
    entity: Literal["product", "customer"]
    raw_alias: str
    normalized_alias: str
    target_id: int
    customer_id: int | None = None
    created_by: str = "system"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    usage_count: int = 0
    active: bool = True


def _new_alias_id() -> str:
    return uuid.uuid4().hex[:12]


class AliasStore:
    """Persistent alias storage backed by JSON files.

    Files live under ``logs/aliases/<entity>_aliases.json``. Writes are atomic
    (tmp file + os.replace) and guarded by a re-entrant lock; loads are cached
    by file mtime so external staff edits are picked up automatically.

    Aliases are only ever created through :meth:`create_product` /
    :meth:`create_customer`, intended for future staff-correction tooling.
    Nothing in the resolution layer auto-creates aliases from low-confidence
    matches.
    """

    ENTITIES = ("product", "customer")

    def __init__(self, directory: str | Path | None = None):
        self.directory = (
            Path(directory)
            if directory
            else Path(get_settings().log_dir) / "aliases"
        )
        ensure_directory(self.directory)
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, dict[str, AliasRecord]]] = {}

    def _path(self, entity: str) -> Path:
        return self.directory / f"{entity}_aliases.json"

    def _load(self, entity: str) -> dict[str, AliasRecord]:
        path = self._path(entity)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cached = self._cache.get(entity)
        if cached and cached[0] == mtime:
            return cached[1]
        records: dict[str, AliasRecord] = {}
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                for item in payload.get("aliases", []):
                    try:
                        record = AliasRecord.model_validate(item)
                        records[record.id] = record
                    except Exception:
                        logger.warning("alias.record_invalid", entity=entity)
            except json.JSONDecodeError:
                logger.exception("alias.store_corrupt", entity=entity)
        self._cache[entity] = (mtime, records)
        return records

    def _save(self, entity: str, records: dict[str, AliasRecord]) -> None:
        path = self._path(entity)
        payload = {
            "version": 1,
            "aliases": [r.model_dump() for r in records.values()],
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        self._cache[entity] = (mtime, records)

    # ------------------------------------------------------------- lookups

    @staticmethod
    def _best(records: list[AliasRecord]) -> AliasRecord | None:
        if not records:
            return None
        return sorted(
            records,
            key=lambda r: (-r.usage_count, r.created_at, r.id),
        )[0]

    def find_product(
        self, normalized_alias: str, customer_id: int | None = None
    ) -> AliasRecord | None:
        """Most specific active alias wins: customer-scoped beats global."""
        with self._lock:
            records = [
                r
                for r in self._load("product").values()
                if r.active and r.normalized_alias == normalized_alias
            ]
        scoped = [r for r in records if customer_id and r.customer_id == customer_id]
        if scoped:
            return self._best(scoped)
        return self._best([r for r in records if r.customer_id is None])

    def find_customer(self, normalized_alias: str) -> AliasRecord | None:
        with self._lock:
            records = [
                r
                for r in self._load("customer").values()
                if r.active and r.normalized_alias == normalized_alias
            ]
        return self._best(records)

    # ------------------------------------------------------------ mutations

    def create_product(
        self,
        raw_alias: str,
        target_product_id: int,
        customer_id: int | None = None,
        created_by: str = "system",
    ) -> AliasRecord:
        normalized = normalize_name(raw_alias)
        if not normalized or not target_product_id:
            raise ValueError("alias requires a non-empty alias and a target product")
        with self._lock:
            records = self._load("product")
            existing = next(
                (
                    r
                    for r in records.values()
                    if r.normalized_alias == normalized
                    and r.customer_id == customer_id
                ),
                None,
            )
            if existing:
                if existing.target_id != target_product_id:
                    raise AliasConflictError(
                        f"alias {normalized!r} already maps to product {existing.target_id}"
                    )
                if not existing.active:
                    existing.active = True
                    self._save("product", records)
                return existing
            record = AliasRecord(
                id=_new_alias_id(),
                entity="product",
                raw_alias=raw_alias.strip(),
                normalized_alias=normalized,
                target_id=target_product_id,
                customer_id=customer_id,
                created_by=created_by,
            )
            records[record.id] = record
            self._save("product", records)
            return record

    def create_customer(
        self,
        raw_alias: str,
        target_partner_id: int,
        created_by: str = "system",
    ) -> AliasRecord:
        normalized = normalize_name(raw_alias)
        if not normalized or not target_partner_id:
            raise ValueError("alias requires a non-empty alias and a target partner")
        with self._lock:
            records = self._load("customer")
            existing = next(
                (r for r in records.values() if r.normalized_alias == normalized),
                None,
            )
            if existing:
                if existing.target_id != target_partner_id:
                    raise AliasConflictError(
                        f"alias {normalized!r} already maps to partner {existing.target_id}"
                    )
                if not existing.active:
                    existing.active = True
                    self._save("customer", records)
                return existing
            record = AliasRecord(
                id=_new_alias_id(),
                entity="customer",
                raw_alias=raw_alias.strip(),
                normalized_alias=normalized,
                target_id=target_partner_id,
                created_by=created_by,
            )
            records[record.id] = record
            self._save("customer", records)
            return record

    def record_usage(self, entity: str, alias_id: str) -> None:
        with self._lock:
            records = self._load(entity)
            record = records.get(alias_id)
            if not record:
                return
            record.usage_count += 1
            self._save(entity, records)

    def deactivate(self, entity: str, alias_id: str, deactivated_by: str = "system") -> bool:
        with self._lock:
            records = self._load(entity)
            record = records.get(alias_id)
            if not record:
                return False
            record.active = False
            self._save(entity, records)
            logger.info("alias.deactivated", entity=entity, alias_id=alias_id, by=deactivated_by)
            return True

    def validate_targets(
        self,
        product_ids: set[int] | None = None,
        partner_exists=None,
    ) -> dict[str, list[dict]]:
        """Report active aliases whose targets no longer exist. Read-only.

        ``product_ids``: live catalog ids (None skips the product check).
        ``partner_exists``: callable ``(partner_id) -> bool`` (None skips
        the customer check). Semantic wrongness (right id, wrong unit) can
        only be judged by humans — this catches dangling references from
        catalog rebuilds.
        """
        dead: dict[str, list[dict]] = {"product": [], "customer": []}
        with self._lock:
            products = self._load("product")
            customers = self._load("customer")
        if product_ids is not None:
            live = {int(pid) for pid in product_ids}
            for record in products.values():
                if record.active and int(record.target_id) not in live:
                    dead["product"].append(self._describe(record))
        if partner_exists is not None:
            for record in customers.values():
                if not record.active:
                    continue
                try:
                    alive = bool(partner_exists(int(record.target_id)))
                except Exception:
                    logger.exception("alias.target_check_failed", alias_id=record.id)
                    continue
                if not alive:
                    dead["customer"].append(self._describe(record))
        if dead["product"] or dead["customer"]:
            logger.warning("alias.dead_targets_found",
                           products=len(dead["product"]), customers=len(dead["customer"]))
        return dead

    @staticmethod
    def _describe(record: AliasRecord) -> dict:
        return {"id": record.id, "raw_alias": record.raw_alias,
                "target_id": record.target_id, "usage_count": record.usage_count,
                "created_by": record.created_by, "created_at": record.created_at}

    def list_aliases(
        self,
        entity: str,
        query: str | None = None,
        active_only: bool = True,
    ) -> list[AliasRecord]:
        needle = normalize_name(query)
        with self._lock:
            records = list(self._load(entity).values())
        results = []
        for record in records:
            if active_only and not record.active:
                continue
            if needle and needle not in record.normalized_alias:
                continue
            results.append(record)
        return sorted(results, key=lambda r: r.created_at, reverse=True)
