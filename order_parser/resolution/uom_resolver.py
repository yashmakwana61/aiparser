from __future__ import annotations

import json
from pathlib import Path

import structlog

from order_parser.config import get_settings
from order_parser.resolution.models import (
    APPROVED_CONVERSION,
    EXPLICIT_UOM,
    PRODUCT_SALES_UOM,
    UOMResolution,
    ResolutionStatus,
)
from order_parser.resolution.normalization import normalize_name

logger = structlog.get_logger(__name__)

DEFAULT_UOM_WORDS = {"unit", "units", "nos", "no", "pcs", "pc", "pieces", ""}


def _numeric_uom_id(value) -> int:
    """Odoo read() returns many2one fields as [id, display_name]."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else 0
    return int(value)
DEFAULT_CONVERSIONS_PATH = Path(__file__).resolve().parents[2] / "data" / "uom_conversions.json"


class UOMResolver:
    """Unit-of-measure resolution; never invents a UOM.

    Hierarchy: explicit parsed UOM -> product's configured Sales UOM ->
    staff-approved conversion table -> exception (unresolved). Approved
    conversions carry a multiplicative factor applied to the ordered
    quantity by the caller.
    """

    def __init__(self, odoo, conversions_path: str | Path | None = None, settings=None):
        self.odoo = odoo
        self.settings = settings or get_settings()
        self.conversions_path = Path(conversions_path) if conversions_path else DEFAULT_CONVERSIONS_PATH
        self._conversions: list[dict] | None = None

    def _load_conversions(self) -> list[dict]:
        if self._conversions is not None:
            return self._conversions
        self._conversions = []
        try:
            payload = json.loads(self.conversions_path.read_text(encoding="utf-8"))
            for row in payload.get("conversions", []):
                tokens = {normalize_name(t) for t in row.get("match", [])}
                factor = float(row.get("factor") or 1.0)
                target_id = row.get("to_uom_id")
                target_name = row.get("to_uom_name")
                if tokens and factor > 0 and (target_id or target_name):
                    self._conversions.append(
                        {"tokens": tokens, "to_uom_id": target_id, "to_uom_name": target_name, "factor": factor}
                    )
        except FileNotFoundError:
            logger.info("uom.no_conversions_file", path=str(self.conversions_path))
        except Exception:
            logger.exception("uom.conversions_load_failed")
        return self._conversions

    def resolve(self, parsed_uom: str | None, product_resolution) -> tuple[UOMResolution, float]:
        raw = (parsed_uom or "").strip()
        base = UOMResolution(parsed_uom=raw)

        # Level 1: explicit UOM stated on the order.
        if normalize_name(raw) not in DEFAULT_UOM_WORDS:
            record = self._find_by_name(raw)
            if record is not None:
                base.status = ResolutionStatus.RESOLVED
                base.source = "odoo"
                base.resolution_method = EXPLICIT_UOM
                base.value = record.get("name")
                base.uom_id = record["id"]
                base.uom_name = record.get("name")
                return base, 1.0

            # Level 3: approved conversion table (explicit but non-catalog UOM).
            for conversion in self._load_conversions():
                if normalize_name(raw) in conversion["tokens"]:
                    record = None
                    if conversion["to_uom_id"]:
                        record = self._get_uom(conversion["to_uom_id"])
                    elif conversion["to_uom_name"]:
                        record = self._find_by_name(conversion["to_uom_name"])
                    if record is not None:
                        factor = float(conversion.get("factor") or 1.0)
                        base.status = ResolutionStatus.RESOLVED
                        base.source = "config"
                        base.resolution_method = APPROVED_CONVERSION
                        base.value = record.get("name")
                        base.uom_id = record["id"]
                        base.uom_name = record.get("name")
                        base.conversion_factor = factor
                        base.details = {"approved_by": conversion.get("approved_by")}
                        return base, factor
                    logger.warning("uom.conversion_target_missing", to_uom=conversion.get("to_uom_name"))

        # Level 2: product's configured Sales UOM.
        product_uom_id = (product_resolution.details or {}).get("uom_id") if product_resolution else None
        if product_uom_id:
            record = self._get_uom(product_uom_id)
            if record is not None:
                base.status = ResolutionStatus.RESOLVED
                base.source = "odoo"
                base.resolution_method = PRODUCT_SALES_UOM
                base.value = record.get("name")
                base.uom_id = record["id"]
                base.uom_name = record.get("name")
                return base, 1.0

        # Level 4: exception - unresolved, never guessed.
        base.status = ResolutionStatus.UNRESOLVED
        base.reason = "uom_unresolved"
        return base, 1.0

    def _get_uom(self, uom_id) -> dict | None:
        try:
            return self.odoo.get_uom(_numeric_uom_id(uom_id))
        except Exception:
            logger.exception("uom.fetch_failed", uom_id=uom_id)
            return None

    def _find_by_name(self, name: str) -> dict | None:
        try:
            found = self.odoo.search_uoms([["name", "=ilike", name]], limit=1)
            if found:
                return found[0]
        except Exception:
            logger.exception("uom.search_failed", name=name)
            return None
        try:
            legacy_id = self.odoo.find_uom(name)
            return self._get_uom(legacy_id) if legacy_id else None
        except Exception:
            logger.exception("uom.legacy_search_failed", name=name)
            return None
