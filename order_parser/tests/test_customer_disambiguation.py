"""Address-depth customer disambiguation: GSTIN-decisive, address tiebreak."""

import asyncio
from types import SimpleNamespace

from order_parser.config import Settings
from order_parser.models import CustomerModel
from order_parser.resolution.customer_resolver import CustomerResolver
from order_parser.resolution.models import ResolutionStatus


PARTNERS = {
    1126: {"id": 1126, "name": "ITC HOTELS LIMITED - ITC MAURYA", "city": "New Delhi",
           "zip": "110021", "street": "ITC Maurya, Sardar Patel Marg", "vat": "07AAAH1234A1Z1"},
    1127: {"id": 1127, "name": "ITC HOTELS LIMITED - TAURU", "city": "Tauru",
           "zip": "122105", "street": "ITC Grand Bharat, Tauru", "vat": "06AAAH1234A1Z2"},
}

# Identical names: exact matching always multi-hits -> disambiguation decides.
TWINS = {
    1: {"id": 1, "name": "ABC Traders", "city": "Ahmedabad",
        "zip": "380001", "street": "Ring Road", "vat": "24ABCDE0001A1Z1"},
    2: {"id": 2, "name": "ABC Traders", "city": "Surat",
        "zip": "395001", "street": "Ghod Dod Road", "vat": "24ABCDE0002A1Z2"},
}


class FakeOdoo:
    enabled = True

    def __init__(self, partners=None):
        self._partners = partners if partners is not None else PARTNERS
        self.get_calls = []

    def search_partners(self, domain, limit=2):
        results = []
        for partner in self._partners.values():
            ok = True
            for field, op, value in domain:
                actual = str(partner.get(field) or "")
                if op == "=ilike" and actual.casefold() != str(value).casefold():
                    ok = False
                elif op == "ilike" and str(value).casefold() not in actual.casefold():
                    ok = False
            if ok:
                results.append({"id": partner["id"], "name": partner["name"]})
        return results[:limit]

    def get_partner(self, partner_id):
        self.get_calls.append(int(partner_id))
        partner = self._partners.get(int(partner_id))
        return dict(partner) if partner else None


def _resolver(odoo=None):
    return CustomerResolver(odoo or FakeOdoo(), aliases=None, settings=Settings())


def _customer(name="ITC Hotels Limited", **overrides):
    data = {"name": name}
    data.update(overrides)
    return CustomerModel(**data)


def test_gstin_breaks_name_tie_decisively():
    resolver = _resolver()
    resolved = resolver.resolve(_customer(gstin="06AAAH1234A1Z2"))
    assert resolved.status == ResolutionStatus.RESOLVED
    assert resolved.partner_id == 1127
    assert resolved.resolution_method == "vat_exact"
    assert resolved.confidence == 100.0


def test_address_unique_winner_resolves():
    resolver = _resolver(FakeOdoo(TWINS))
    resolved = resolver.resolve(_customer(
        "ABC Traders", city="Surat", zip_code="395001", address="Ghod Dod Road"))
    assert resolved.status == ResolutionStatus.RESOLVED
    assert resolved.partner_id == 2
    assert resolved.resolution_method == "address_match"


def test_city_only_input_stays_ambiguous():
    resolver = _resolver(FakeOdoo(TWINS))
    resolved = resolver.resolve(_customer("ABC Traders", city="Surat"))
    # City alone scores 60 < 70 bar: safe ambiguity, cities shown for picking.
    assert resolved.status == ResolutionStatus.AMBIGUOUS
    assert resolved.reason == "multiple_partners_match"
    assert {c.get("city") for c in resolved.candidates} == {"Ahmedabad", "Surat"}


def test_identical_addresses_stay_ambiguous():
    twins = {
        1: dict(TWINS[1]),
        2: dict(TWINS[2], city="Ahmedabad", zip="380001",
                street="Ring Road", vat="24ABCDE0002A1Z2"),
    }
    resolver = _resolver(FakeOdoo(twins))
    resolved = resolver.resolve(_customer("ABC Traders", city="Ahmedabad",
                                          zip_code="380001", address="Ring Road"))
    assert resolved.status == ResolutionStatus.AMBIGUOUS
    cities = {c.get("city") for c in resolved.candidates}
    assert "Ahmedabad" in cities


def test_gstin_wins_even_when_addresses_identical():
    twins = {
        1: dict(TWINS[1]),
        2: dict(TWINS[2], city="Ahmedabad", zip="380001", street="Ring Road"),
    }
    resolver = _resolver(FakeOdoo(twins))
    resolved = resolver.resolve(_customer("ABC Traders", city="Ahmedabad",
                                          zip_code="380001", address="Ring Road",
                                          gstin="24ABCDE0002A1Z2"))
    assert resolved.status == ResolutionStatus.RESOLVED
    assert resolved.partner_id == 2
    assert resolved.resolution_method == "vat_exact"


def test_shared_gstin_falls_back_to_address():
    # Sister units share one state GSTIN: the address must pick the unit.
    twins = {
        1: dict(TWINS[1], vat="24SHARED0001A1Z1"),
        2: dict(TWINS[2], vat="24SHARED0001A1Z1"),
    }
    resolver = _resolver(FakeOdoo(twins))
    resolved = resolver.resolve(_customer("ABC Traders", city="Surat",
                                          zip_code="395001", address="Ghod Dod Road",
                                          gstin="24SHARED0001A1Z1"))
    assert resolved.status == ResolutionStatus.RESOLVED
    assert resolved.partner_id == 2
    assert resolved.resolution_method == "address_match"


def test_gstin_address_conflict_stays_ambiguous():
    # GSTIN names twin 2, but the address clearly names twin 1: refuse to guess.
    resolver = _resolver(FakeOdoo(TWINS))
    resolved = resolver.resolve(_customer("ABC Traders", city="Ahmedabad",
                                          zip_code="380001", address="Ring Road Ahmedabad",
                                          gstin="24ABCDE0002A1Z2"))
    assert resolved.status == ResolutionStatus.AMBIGUOUS


def test_name_only_input_stays_ambiguous():
    fake = FakeOdoo(TWINS)
    resolver = CustomerResolver(fake, aliases=None, settings=Settings())
    resolved = resolver.resolve(_customer("ABC Traders"))
    assert resolved.status == ResolutionStatus.AMBIGUOUS
    assert resolved.reason == "multiple_partners_match"
    # Candidates still carry cities (single bounded enrichment pass) for picking.
    assert {c.get("city") for c in resolved.candidates} == {"Ahmedabad", "Surat"}


def test_wrong_gstin_does_not_force_match():
    resolver = _resolver(FakeOdoo(TWINS))
    resolved = resolver.resolve(_customer("ABC Traders", gstin="99XXXX0000X1Z9",
                                          city="Surat", zip_code="395001",
                                          address="Ghod Dod Road Surat"))
    # GSTIN matches nobody; address still uniquely identifies Surat.
    assert resolved.status == ResolutionStatus.RESOLVED
    assert resolved.partner_id == 2


def test_get_partner_reads_address_fields(monkeypatch):
    from order_parser.integrations import odoo_client as oc

    captured = {}

    class FakeProxy:
        def execute_kw(self, *args):
            captured["args"] = args
            return [{"id": 1, "name": "X"}]

    client = oc.OdooClient(url="http://x", db="d", username="u", password="p")
    monkeypatch.setattr(client, "_proxy_models", lambda: FakeProxy())
    monkeypatch.setattr(client, "authenticate", lambda: 1)
    assert client.get_partner(1) == {"id": 1, "name": "X"}
    # Proxy receives (db, uid, password, model, method, args, kwargs).
    fields = captured["args"][5][1]
    assert {"street", "city", "zip", "vat"} <= set(fields)
    # This Odoo build rejects non-core fields (mobile, ...) with Invalid
    # field, which used to break every partner read.
    assert "mobile" not in set(fields)


def test_start_and_help_answer_globally():
    from order_parser.channels.telegram_handler import TelegramHandler

    pipeline = SimpleNamespace(pending_store=None, resolver=None, odoo=None,
                               process=lambda *a, **k: {}, confirm_order=lambda *a, **k: {},
                               reject_order=lambda *a, **k: {})

    class FakeMessage:
        def __init__(self, text):
            self.text = text
            self.chat_id = 1
            self.message_id = 1
            self.from_user = SimpleNamespace(id=99, full_name="Zed")
            self.photo = []
            self.document = None
            self.replies = []

        async def reply_text(self, text, **kwargs):
            self.replies.append(text)

    handler = TelegramHandler(pipeline)
    start = FakeMessage("/start")
    asyncio.run(handler.handle_update(
        SimpleNamespace(effective_message=start, effective_user=start.from_user,
                        callback_query=None), None))
    assert any("Welcome" in r for r in start.replies)
    help_msg = FakeMessage("/help")
    asyncio.run(handler.handle_update(
        SimpleNamespace(effective_message=help_msg, effective_user=help_msg.from_user,
                        callback_query=None), None))
    assert any("/status" in r for r in help_msg.replies)
