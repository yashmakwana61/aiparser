from __future__ import annotations

import structlog

from order_parser.config import get_settings
from order_parser.models import CustomerModel
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.models import (
    ALIAS_MATCH,
    DEFAULT_FUZZY_CONFIDENCE_CAP,
    EMAIL_EXACT,
    EXACT_NAME,
    EXPLICIT_ID,
    FUZZY_MATCH,
    PHONE_EXACT,
    SESSION_CUSTOMER,
    STAFF_SELECTED,
    CustomerResolution,
    ResolutionStatus,
)
from order_parser.resolution.normalization import normalize_name
from order_parser.resolution.product_resolver import fuzzy_score

logger = structlog.get_logger(__name__)

MAX_CANDIDATES = 5


class CustomerResolver:
    """Deterministic customer identity resolution against Odoo partners.

    Hierarchy: explicit id/reference -> session customer -> staff-selected ->
    exact name -> email -> phone -> alias -> fuzzy match (capped below the
    auto-create band) -> exception. Multiple equally-plausible matches are
    reported as ``ambiguous``. This resolver NEVER creates a new partner;
    unknown customers surface as an unresolved blocking issue for manual
    handling.
    """

    def __init__(self, odoo, aliases: AliasStore | None = None, settings=None):
        self.odoo = odoo
        self.aliases = aliases
        self.settings = settings or get_settings()
        self.min_score = float(self.settings.resolution_fuzzy_cutoff)
        self.ambiguity_gap = float(self.settings.resolution_ambiguity_gap)
        self.confidence_cap = float(
            getattr(self.settings, "resolution_fuzzy_confidence_cap", DEFAULT_FUZZY_CONFIDENCE_CAP)
        )

    def resolve(
        self,
        customer: CustomerModel,
        session_partner_id: int | None = None,
        staff_partner_id: int | None = None,
        explicit_reference: int | str | None = None,
    ) -> CustomerResolution:
        name = (customer.name or "").strip()
        email = (customer.email or "").strip()
        phone = (customer.phone or "").strip()

        # Levels 1-3: explicit identifiers (verified against Odoo; dead
        # references fail hard rather than silently falling through).
        for value, method in (
            (explicit_reference, EXPLICIT_ID),
            (session_partner_id, SESSION_CUSTOMER),
            (staff_partner_id, STAFF_SELECTED),
        ):
            if value is None or value == "":
                continue
            try:
                partner_id = int(value)
            except (TypeError, ValueError):
                return self._unresolved("invalid_customer_reference", name=name, email=email, phone=phone)
            partner = self._get_partner(partner_id)
            if partner is None:
                logger.warning("customer.reference_not_found", method=method, partner_id=partner_id)
                return self._unresolved("invalid_customer_reference", name=name, email=email, phone=phone)
            return CustomerResolution(
                status=ResolutionStatus.RESOLVED,
                source="odoo",
                resolution_method=method,
                value=partner.get("name"),
                confidence=100.0,
                reference_id=partner["id"],
                partner_id=partner["id"],
                partner_name=partner.get("name"),
            )

        if not name and not email and not phone:
            return self._unresolved("customer_info_missing")

        if not getattr(self.odoo, "enabled", True):
            return self._unresolved("odoo_unavailable", name=name, email=email, phone=phone)

        # Level 4/5/6: exact field lookups; >1 identical hit means ambiguous.
        for domain, method in (
            ([["name", "=ilike", name]], EXACT_NAME) if name else (None, None),
            ([["email", "=ilike", email]], EMAIL_EXACT) if email else (None, None),
            ([["phone", "=ilike", phone]], PHONE_EXACT) if phone else (None, None),
        ):
            if domain is None:
                continue
            found = self._search(domain)
            if len(found) == 1:
                partner = found[0]
                return CustomerResolution(
                    status=ResolutionStatus.RESOLVED,
                    source="odoo",
                    resolution_method=method,
                    value=partner.get("name"),
                    confidence=100.0,
                    reference_id=partner["id"],
                    partner_id=partner["id"],
                    partner_name=partner.get("name"),
                )
            if len(found) > 1:
                candidates = [
                    {"partner_id": p["id"], "name": p.get("name"), "score": 100.0, "method": method}
                    for p in found[:MAX_CANDIDATES]
                ]
                return CustomerResolution(
                    status=ResolutionStatus.AMBIGUOUS,
                    source="odoo",
                    reason="multiple_partners_match",
                    candidates=candidates,
                )

        # Level 7: customer alias store.
        if self.aliases is not None and name:
            alias = self.aliases.find_customer(normalize_name(name))
            if alias is not None:
                partner = self._get_partner(alias.target_id)
                if partner is not None:
                    self.aliases.record_usage("customer", alias.id)
                    return CustomerResolution(
                        status=ResolutionStatus.RESOLVED,
                        source="alias_store",
                        resolution_method=ALIAS_MATCH,
                        value=partner.get("name"),
                        confidence=98.0,
                        reference_id=partner["id"],
                        partner_id=partner["id"],
                        partner_name=partner.get("name"),
                    )
                logger.warning("customer.alias_target_missing", alias_id=alias.id, target_id=alias.target_id)

        # Level 8: fuzzy matching over a prefiltered candidate set.
        if name:
            token = normalize_name(name).split()[0] if normalize_name(name) else ""
            pool = self._search([["name", "ilike", token]], limit=50) if token else []
            scored: list[tuple[float, dict]] = []
            for partner in pool:
                score = fuzzy_score(name, str(partner.get("name") or ""))
                if score >= self.min_score:
                    scored.append((round(float(score), 1), partner))
            scored.sort(key=lambda pair: (-pair[0], pair[1].get("id") or 0))
            if scored:
                best_score, best_partner = scored[0]
                if len(scored) > 1 and (best_score - scored[1][0]) <= self.ambiguity_gap:
                    return CustomerResolution(
                        status=ResolutionStatus.AMBIGUOUS,
                        source="odoo",
                        reason="fuzzy_candidates_too_close",
                        candidates=[
                            {"partner_id": p["id"], "name": p.get("name"), "score": s, "method": FUZZY_MATCH}
                            for s, p in scored[:MAX_CANDIDATES]
                        ],
                    )
                return CustomerResolution(
                    status=ResolutionStatus.RESOLVED,
                    source="odoo",
                    resolution_method=FUZZY_MATCH,
                    value=best_partner.get("name"),
                    confidence=min(best_score, self.confidence_cap),
                    reference_id=best_partner["id"],
                    partner_id=best_partner["id"],
                    partner_name=best_partner.get("name"),
                    details={"candidates": [
                        {"partner_id": p["id"], "name": p.get("name"), "score": s} for s, p in scored[:MAX_CANDIDATES]
                    ]},
                )

        # Level 9: exception - never create a new customer automatically.
        return self._unresolved("no_matching_customer", name=name, email=email, phone=phone)

    def _get_partner(self, partner_id: int) -> dict | None:
        try:
            return self.odoo.get_partner(partner_id)
        except Exception:
            logger.exception("customer.partner_fetch_failed", partner_id=partner_id)
            return None

    def _search(self, domain: list, limit: int = 2) -> list[dict]:
        try:
            return list(self.odoo.search_partners(domain, limit=limit) or [])
        except Exception:
            logger.exception("customer.partner_search_failed", domain=domain)
            return []

    @staticmethod
    def _unresolved(reason: str, **kwargs) -> CustomerResolution:
        return CustomerResolution(
            status=ResolutionStatus.UNRESOLVED,
            reason=reason,
            details={f"raw_{k}": v for k, v in kwargs.items()},
        )
