"""Safety net + normalized matching + near-miss candidates."""

from order_parser.config import Settings
from order_parser.models import CustomerModel
from order_parser.resolution.customer_resolver import CustomerResolver
from order_parser.resolution.models import ResolutionStatus


class FakeOdoo:
    enabled = True

    def __init__(self, partners):
        self._partners = partners

    def search_partners(self, domain, limit=2, fields=None):
        results = []
        for partner in self._partners.values():
            ok = True
            for field, op, value in domain:
                actual = partner.get(field)
                if op == "=ilike" and str(actual or "").casefold() != str(value).casefold():
                    ok = False
                elif op == "ilike" and str(value).casefold() not in str(actual or "").casefold():
                    ok = False
                elif op == "in" and actual not in (value or []):
                    ok = False
            if ok:
                results.append({"id": partner["id"], "name": partner["name"]})
        return results[:limit]

    def get_partner(self, partner_id):
        partner = self._partners.get(int(partner_id))
        return dict(partner) if partner else None


PARTNERS = {
    1717: {"id": 1717, "name": "ONLY COFFEE NOTHING ELSE", "city": "",
           "zip": "", "street": "", "vat": ""},
}


def _resolver(partners=None):
    return CustomerResolver(FakeOdoo(partners or PARTNERS), aliases=None,
                            settings=Settings())


def test_comma_and_case_variants_resolve():
    resolver = _resolver()
    for raw in ("only coffee, nothing else", "Only Coffee Nothing Else",
                "only  coffee   nothing else", "ONLY COFFEE NOTHING ELSE"):
        resolved = resolver.resolve(CustomerModel(name=raw))
        assert resolved.status == ResolutionStatus.RESOLVED, raw
        assert resolved.partner_id == 1717
    comma = resolver.resolve(CustomerModel(name="only coffee, nothing else"))
    assert comma.resolution_method == "normalized_name"


def test_normalized_fuzzy_bridges_case_gap():
    partners = {9: {"id": 9, "name": "HOTEL SUPPLIES CO", "city": "",
                   "zip": "", "street": "", "vat": ""}}
    resolver = _resolver(partners)
    resolved = resolver.resolve(CustomerModel(name="hotel supplies co."))
    assert resolved.status == ResolutionStatus.RESOLVED
    assert resolved.partner_id == 9


def test_unresolved_carries_display_candidates():
    # Cutoff raised so an 85.5 near-miss stays unresolved but offered.
    partners = {9: {"id": 9, "name": "Hotel Supplies Corporation", "city": "Delhi",
                   "zip": "", "street": "", "vat": ""}}
    resolver = CustomerResolver(FakeOdoo(partners), aliases=None,
                                settings=Settings(resolution_fuzzy_cutoff=95.0))
    resolved = resolver.resolve(CustomerModel(name="Hotel Zed"))
    assert resolved.status == ResolutionStatus.UNRESOLVED
    assert resolved.candidates, "near miss must be offered, not hidden"
    assert resolved.candidates[0]["partner_id"] == 9


def test_garbage_name_has_no_candidates():
    resolver = _resolver()
    resolved = resolver.resolve(CustomerModel(name="xqzt klmnop"))
    assert resolved.status == ResolutionStatus.UNRESOLVED
    assert resolved.candidates == []
