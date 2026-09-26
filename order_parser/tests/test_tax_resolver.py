from order_parser.config import Settings
from order_parser.resolution.models import (
    TAX_CONFLICT,
    ProductResolution,
    ResolutionStatus,
)
from order_parser.resolution.tax_resolver import TaxResolver


class FakeTaxOdoo:
    enabled = True

    def __init__(self, fiscal_positions=None, mapped=None, defaults=None):
        self._fpos = fiscal_positions or {}
        self._mapped = mapped or {}
        self._defaults = defaults or []

    def get_partner_fiscal_position(self, partner_id):
        return self._fpos.get(partner_id)

    def map_taxes_through_fiscal_position(self, fiscal_position_id, tax_ids):
        if not tax_ids:
            return []
        return [self._mapped.get(t, t) for t in tax_ids]

    def default_sale_taxes(self):
        return list(self._defaults)


def _settings(**overrides):
    defaults = {"trust_explicit_taxes": False}
    defaults.update(overrides)
    return Settings(**defaults)


def _product(taxes):
    return ProductResolution(
        status=ResolutionStatus.RESOLVED,
        product_id=123,
        details={"taxes_id": taxes},
    )


def test_product_tax_resolved():
    result = TaxResolver(FakeTaxOdoo(), _settings()).resolve(None, _product([5]), None)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "product_tax"
    assert result.tax_ids == [5]


def test_fiscal_position_remaps_product_tax():
    odoo = FakeTaxOdoo(fiscal_positions={42: 3}, mapped={5: 6})
    result = TaxResolver(odoo, _settings()).resolve(None, _product([5]), 42)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "customer_fiscal_position"
    assert result.fiscal_position_id == 3
    assert result.tax_ids == [6]


def test_fiscal_position_can_exempt_when_no_product_tax():
    odoo = FakeTaxOdoo(fiscal_positions={42: 3})
    result = TaxResolver(odoo, _settings()).resolve(None, _product([]), 42)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "customer_fiscal_position"
    assert result.tax_ids == []


def test_company_default_tax_is_last_odoo_source():
    odoo = FakeTaxOdoo(defaults=[9])
    result = TaxResolver(odoo, _settings()).resolve(None, _product([]), None)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "company_default"
    assert result.tax_ids == [9]


def test_missing_tax_unresolved():
    result = TaxResolver(FakeTaxOdoo(), _settings()).resolve(None, _product([]), None)
    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason == "tax_unresolved"


def test_trusted_explicit_tax_accepted_when_enabled():
    odoo = FakeTaxOdoo(defaults=[])
    result = TaxResolver(odoo, _settings(trust_explicit_taxes=True)).resolve(["7"], _product([]), None)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "explicit_tax"
    assert result.explicit_tax_names == ["7"]
    assert result.tax_ids == [7]


def test_trusted_explicit_tax_conflicting_with_product_blocks():
    odoo = FakeTaxOdoo()
    result = TaxResolver(odoo, _settings(trust_explicit_taxes=True)).resolve(["6"], _product([5]), None)
    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.reason == TAX_CONFLICT


def test_untrusted_explicit_tax_ignored_in_favour_of_product_tax():
    result = TaxResolver(FakeTaxOdoo(), _settings()).resolve(["GST 18%"], _product([5]), None)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolution_method == "product_tax"
    assert result.tax_ids == [5]
