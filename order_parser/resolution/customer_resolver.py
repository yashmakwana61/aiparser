from __future__ import annotations

import structlog

from order_parser.config import get_settings
from order_parser.models import CustomerModel
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.models import (
    ADDRESS_MATCH,
    ALIAS_MATCH,
    DEFAULT_FUZZY_CONFIDENCE_CAP,
    EMAIL_EXACT,
    EXACT_NAME,
    EXPLICIT_ID,
    FUZZY_MATCH,
    PHONE_EXACT,
    SESSION_CUSTOMER,
    STAFF_SELECTED,
    VAT_EXACT,
    CustomerResolution,
    ResolutionStatus,
)
from order_parser.resolution.normalization import normalize_name
from order_parser.resolution.product_resolver import fuzzy_score

logger = structlog.get_logger(__name__)

MAX_CANDIDATES = 5

# Address tiebreaker: unique winner must clear this bar AND match on zip or
# city. Anything less stays ambiguous — the resolver never guesses.
ADDRESS_WIN_SCORE = 70.0


def _norm_tax_id(value: str | None) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _address_score(customer: CustomerModel, partner: dict) -> tuple[float, bool, bool]:
    """Score 0-100 how well the input address matches a partner record."""
    zip_match = bool(customer.zip_code) and normalize_name(customer.zip_code) == normalize_name(
        partner.get("zip"))
    city_in, city_out = normalize_name(customer.city), normalize_name(partner.get("city"))
    city_match = bool(city_in and city_out) and (city_in in city_out or city_out in city_in)
    in_tokens = set((normalize_name(customer.address) + " " + normalize_name(customer.city)).split())
    out_tokens = set(
        (normalize_name(partner.get("street")) + " " + normalize_name(partner.get("street2")) + " "
         + normalize_name(partner.get("city"))).split())
    overlap = len(in_tokens & out_tokens) / max(1, len(in_tokens)) if in_tokens else 0.0
    score = 40.0 * zip_match + 30.0 * city_match + 30.0 * overlap
    return round(score, 1), bool(zip_match), bool(city_match)


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
        gstin = _norm_tax_id(customer.gstin)

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

        # Level 4/5/6: exact field lookups. A unique hit resolves
        # immediately; multiple hits go through GSTIN/address disambiguation
        # before falling back to ambiguous. (GSTIN intentionally has no
        # standalone level: one state GSTIN is often shared by sister units,
        # so it may only narrow candidates, never pick a unit alone.)
        for domain, method in (
            ([["name", "=ilike", name]], EXACT_NAME) if name else (None, None),
            ([["email", "=ilike", email]], EMAIL_EXACT) if email else (None, None),
            ([["phone", "=ilike", phone]], PHONE_EXACT) if phone else (None, None),
        ):
            if domain is None:
                continue
            # Wider pool than the display cap: disambiguation scores across
            # all of these, so a longer-named unit is never cut off early.
            found = self._search(domain, limit=25)
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
                targeted = self._targeted_pool(customer)
                known = {c.get("partner_id", c.get("id")) for c in found}
                pool = list(found) + [t for t in targeted
                                      if t.get("partner_id") not in known]
                disambiguated = self._disambiguate(
                    customer, pool, method, reason="multiple_partners_match")
                if disambiguated is not None:
                    return disambiguated
                candidates = [
                    {"partner_id": p["id"], "name": p.get("name"), "score": 100.0, "method": method}
                    for p in found[:MAX_CANDIDATES]
                ]
                return CustomerResolution(
                    status=ResolutionStatus.AMBIGUOUS,
                    source="odoo",
                    reason="multiple_partners_match",
                    candidates=self._with_cities(candidates),
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
                    # Pass the whole scored pool (not just the top pair): the
                    # true unit often ranks below shorter sibling names, and
                    # address scoring across the pool can still find it.
                    pool = [
                        {"partner_id": p["id"], "name": p.get("name"), "score": s, "method": FUZZY_MATCH}
                        for s, p in scored
                    ]
                    for targeted in self._targeted_pool(customer):
                        if all(t.get("partner_id") != targeted["partner_id"] for t in pool):
                            pool.append(targeted)
                    disambiguated = self._disambiguate(
                        customer, pool, FUZZY_MATCH, reason="fuzzy_candidates_too_close")
                    if disambiguated is not None:
                        return disambiguated
                    close = [
                        {"partner_id": p["id"], "name": p.get("name"), "score": s, "method": FUZZY_MATCH}
                        for s, p in scored[:MAX_CANDIDATES]
                    ]
                    return CustomerResolution(
                        status=ResolutionStatus.AMBIGUOUS,
                        source="odoo",
                        reason="fuzzy_candidates_too_close",
                        candidates=self._with_cities(close),
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

    def _fetch_details(self, ids: list[int]) -> dict[int, dict]:
        """Partner address records in one round-trip when supported.

        Falls back to per-id reads for clients/stores without field support
        (older signatures, test fakes). Never raises: gaps simply shrink
        the disambiguation pool.
        """
        unique = list(dict.fromkeys(int(i) for i in ids if i is not None))
        if not unique:
            return {}
        try:
            rows = self.odoo.search_partners(
                [["id", "in", unique]], limit=len(unique),
                fields=["id", "name", "street", "street2", "city", "zip", "vat"],
            )
            found = {int(r["id"]): dict(r) for r in rows or [] if r.get("id") is not None}
            if len(found) == len(unique):
                return found
        except Exception:
            logger.debug("customer.bulk_details_unsupported")
        details: dict[int, dict] = {}
        for partner_id in unique:
            partner = self._get_partner(partner_id)
            if partner:
                details[partner_id] = partner
        return details

    def _disambiguate(self, customer: CustomerModel, candidates: list[dict],
                        method: str, reason: str) -> CustomerResolution | None:
        """Break a name tie using GSTIN, then address. None = still ambiguous.

        Runs only when the input carries validation material (GSTIN or an
        address); otherwise no extra Odoo reads happen. A unique, decisive
        match resolves; ties and weak scores return None so the caller keeps
        the order in human review.
        """
        gstin = _norm_tax_id(customer.gstin)
        has_address = bool((customer.address or "").strip() or (customer.city or "").strip()
                           or (customer.zip_code or "").strip())
        if not gstin and not has_address:
            return None
        ids: list[int] = []
        for candidate in candidates[:MAX_CANDIDATES * 10]:
            try:
                # Search hits carry "id"; scored candidate dicts carry "partner_id".
                ids.append(int(candidate.get("partner_id", candidate.get("id"))))
            except (TypeError, ValueError):
                continue
        details = self._fetch_details(ids)
        if not details:
            return None
        if gstin:
            hits = [pid for pid, partner in details.items()
                    if _norm_tax_id(partner.get("vat")) == gstin]
            if len(hits) == 1 and not has_address:
                # GSTIN is the only signal: decisive for the company record.
                partner = details[hits[0]]
                logger.info("customer.gstin_disambiguated", partner_id=hits[0])
                return CustomerResolution(
                    status=ResolutionStatus.RESOLVED,
                    source="odoo",
                    resolution_method=VAT_EXACT,
                    value=partner.get("name"),
                    confidence=100.0,
                    reference_id=hits[0],
                    partner_id=hits[0],
                    partner_name=partner.get("name"),
                    details={"disambiguated_from": method, "via": "gstin"},
                )
            if len(hits) == 1 and has_address:
                # The GSTIN names one candidate; the address must not clearly
                # point at a different unit — that conflict stays in review.
                scored = {pid: _address_score(customer, partner)[0]
                          for pid, partner in details.items()}
                hit_score = scored[hits[0]]
                if all(score <= hit_score for pid, score in scored.items() if pid != hits[0]):
                    partner = details[hits[0]]
                    logger.info("customer.gstin_address_confirmed", partner_id=hits[0])
                    return CustomerResolution(
                        status=ResolutionStatus.RESOLVED,
                        source="odoo",
                        resolution_method=VAT_EXACT,
                        value=partner.get("name"),
                        confidence=100.0,
                        reference_id=hits[0],
                        partner_id=hits[0],
                        partner_name=partner.get("name"),
                        details={"disambiguated_from": method, "via": "gstin+address"},
                    )
                logger.warning("customer.gstin_address_conflict", gstin_hit=hits[0])
                return None
            if len(hits) > 1:
                # A GSTIN identifies the company, not the unit: several units
                # may share it. Narrow to the sharers, then let the address
                # pick the unit — or stay ambiguous.
                logger.info("customer.gstin_shared_by_candidates", count=len(hits))
                pool = {pid: details[pid] for pid in hits}
                winner = self._address_winner(customer, pool)
                if winner is not None:
                    best_id, best_partner, best_score = winner
                    return self._resolved_address(
                        best_id, best_partner, best_score, method)
                return None
        if has_address:
            winner = self._address_winner(customer, details)
            if winner is not None:
                best_id, best_partner, best_score = winner
                return self._resolved_address(best_id, best_partner, best_score, method)
        return None

    @staticmethod
    def _address_winner(customer: CustomerModel,
                        details: dict[int, dict]) -> tuple[int, dict, float] | None:
        """Unique address winner strictly above the runner-up and the bar,
        anchored on a zip or city match. None when undecidable."""
        scored = [
            (_address_score(customer, partner)[0], pid, partner)
            for pid, partner in details.items()
        ]
        if not scored:
            return None
        scored.sort(key=lambda triple: (-triple[0], triple[1]))
        best_score, best_id, best_partner = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else -1.0
        _, zip_match, city_match = _address_score(customer, best_partner)
        if best_score > runner_up and best_score >= ADDRESS_WIN_SCORE and (zip_match or city_match):
            return best_id, best_partner, best_score
        return None

    def _resolved_address(self, partner_id: int, partner: dict,
                          score: float, method: str) -> CustomerResolution:
        logger.info("customer.address_disambiguated", partner_id=partner_id,
                    score=score, from_method=method)
        return CustomerResolution(
            status=ResolutionStatus.RESOLVED,
            source="odoo",
            resolution_method=ADDRESS_MATCH,
            value=partner.get("name"),
            confidence=min(score, self.confidence_cap),
            reference_id=partner_id,
            partner_id=partner_id,
            partner_name=partner.get("name"),
            details={"disambiguated_from": method, "via": "address", "score": score},
        )

    def _targeted_pool(self, customer: CustomerModel) -> list[dict]:
        """Seed candidates directly from location/ID evidence.

        Name-fuzzy pools are ordered by id and capped, so a long-named unit
        can be cut off before scoring. When the input carries a zip, city or
        GSTIN, search those fields directly (small, selective lookups) and
        union the hits into the disambiguation pool.
        """
        seen: dict[int, dict] = {}
        wants: list[tuple[str, str]] = []
        if _norm_tax_id(customer.gstin):
            wants.append(("vat", customer.gstin.strip()))
        if (customer.zip_code or "").strip():
            wants.append(("zip", customer.zip_code.strip()))
        if (customer.city or "").strip():
            wants.append(("city", customer.city.strip()))
        for field_name, value in wants:
            try:
                rows = self.odoo.search_partners(
                    [[field_name, "=ilike", value]], limit=25,
                    fields=["id", "name", "street", "street2", "city", "zip", "vat"],
                )
            except Exception:
                logger.debug("customer.targeted_search_failed", field=field_name)
                continue
            for row in rows or []:
                try:
                    pid = int(row.get("id"))
                except (TypeError, ValueError):
                    continue
                seen.setdefault(pid, {"partner_id": pid, "name": row.get("name"),
                                      "score": None, "method": "targeted_search"})
        return list(seen.values())
        """Enrich ambiguous candidates with their city for pick buttons."""
        enriched = []
        for candidate in candidates:
            entry = dict(candidate)
            try:
                partner = self._get_partner(int(candidate.get("partner_id")))
            except (TypeError, ValueError):
                partner = None
            city = (partner or {}).get("city") or ""
            if city:
                entry["city"] = str(city)
            enriched.append(entry)
        return enriched

    def _with_cities(self, candidates: list[dict]) -> list[dict]:
        """Enrich ambiguous candidates with their city for pick buttons."""
        enriched = []
        for candidate in candidates:
            entry = dict(candidate)
            try:
                partner = self._get_partner(int(candidate.get("partner_id", candidate.get("id"))))
            except (TypeError, ValueError):
                partner = None
            city = (partner or {}).get("city") or ""
            if city:
                entry["city"] = str(city)
            enriched.append(entry)
        return enriched

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
