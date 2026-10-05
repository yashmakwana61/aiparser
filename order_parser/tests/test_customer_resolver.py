from order_parser.config import Settings
from order_parser.models import CustomerModel
from order_parser.resolution.alias_store import AliasStore
from order_parser.resolution.customer_resolver import CustomerResolver
from order_parser.resolution.models import ResolutionStatus


class FakeCustomerOdoo:
    enabled = True

    def __init__(self, partners):
        self._partners = list(partners)

    def get_partner(self, partner_id):
        return next((p for p in self._partners if p["id"] == partner_id), None)

    def search_partners(self, domain, limit=2):
        results = []
        for partner in self._partners:
            ok = True
            for field, op, value in domain:
                actual = str(partner.get(field) or "")
                if op == "=ilike":
                    if actual.casefold() != str(value).casefold():
                        ok = False
                elif op == "ilike":
                    if str(value).casefold() not in actual.casefold():
                        ok = False
                else:
                    ok = False
            if ok:
                results.append({"id": partner["id"], "name": partner.get("name")})
        return results[:limit]

    def create_partner(self, customer):
        raise AssertionError("create_partner must never be called by the resolution layer")


PARTNERS = [
    {"id": 42, "name": "Existing Co", "email": "buyer@existing.com", "phone": "+15551234567"},
    {"id": 43, "name": "Other Traders", "email": "sales@other.com", "phone": "+15550000000"},
]


def _resolver(partners=PARTNERS, alias_store=None):
    settings = Settings()
    return CustomerResolver(FakeCustomerOdoo(partners), alias_store, settings)


def test_exact_name_match():
    result = _resolver().resolve(CustomerModel(name="existing co"))
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "exact_name"
    assert result.partner_id == 42


def test_email_match():
    result = _resolver().resolve(CustomerModel(email="BUYER@existing.com"))
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "email_exact"
    assert result.partner_id == 42


def test_phone_match():
    result = _resolver().resolve(CustomerModel(phone="+15550000000"))
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "phone_exact"
    assert result.partner_id == 43


def test_customer_alias_match(tmp_path):
    store = AliasStore(tmp_path)
    store.create_customer("acme corp", 43)
    result = _resolver(alias_store=store).resolve(CustomerModel(name="ACME Corp!"))
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "alias_match"
    assert result.partner_id == 43
    record = store.list_aliases("customer")[0]
    assert record.usage_count == 1


def test_duplicate_names_are_ambiguous():
    duplicated = PARTNERS + [{"id": 44, "name": "Existing Co", "email": "", "phone": ""}]
    result = _resolver(duplicated).resolve(CustomerModel(name="EXISTING CO"))
    assert result.status == ResolutionStatus.AMBIGUOUS
    assert {c["partner_id"] for c in result.candidates} == {42, 44}


def test_fuzzy_match_is_capped_below_auto_band():
    # Genuinely fuzzy (later-word typo keeps the pool token intact):
    # normalized forms differ, so fuzzy matching handles it, capped.
    result = _resolver().resolve(CustomerModel(name="Existing Cx"))
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "fuzzy_match"
    assert result.confidence <= 89.0


def test_normalized_punctuation_variant_resolves_deterministically():
    # Trailing period is cosmetic: normalized exact wins at 99, not fuzzy.
    result = _resolver().resolve(CustomerModel(name="Existing Co."))
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "normalized_name"
    assert result.partner_id == 42


def test_invalid_explicit_reference_fails_hard():
    result = _resolver().resolve(CustomerModel(name="Existing Co"), explicit_reference=999)
    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason == "invalid_customer_reference"


def test_valid_explicit_reference_wins(tmp_path):
    result = _resolver().resolve(CustomerModel(), explicit_reference="43")
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "explicit_id"
    assert result.partner_id == 43


def test_session_and_staff_selection_precedence():
    resolver = _resolver()
    session = resolver.resolve(CustomerModel(), session_partner_id=42, staff_partner_id=43)
    assert session.resolution_method == "session_customer"
    staff_only = resolver.resolve(CustomerModel(), staff_partner_id=43)
    assert staff_only.resolution_method == "staff_selected"


def test_unknown_customer_never_created():
    result = _resolver().resolve(CustomerModel(name="Mystery Buyer Ltd"))
    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason == "no_matching_customer"


def test_missing_info_unresolved():
    result = _resolver().resolve(CustomerModel())
    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason == "customer_info_missing"
