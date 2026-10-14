from decimal import Decimal

import pytest

from dealhunter.calculator import calculate


def test_original_default_ceiling():
    result = calculate(retail=100)
    assert result['retail_total'] == Decimal('113')
    assert result['max_hammer'] == 26
    assert calculate(retail=100, current=26)['meets_discount']
    assert not calculate(retail=100, current=27)['meets_discount']


@pytest.mark.parametrize('premium_taxable', [True, False])
@pytest.mark.parametrize('shipping_taxable', [True, False])
@pytest.mark.parametrize('fees_taxable', [True, False])
def test_every_tax_treatment_guarantees_70_percent(premium_taxable, shipping_taxable, fees_taxable):
    kwargs = dict(retail='349.99', retail_tax=5, retail_delivery=12,
                  premium=18, auction_tax=13, premium_taxable=premium_taxable,
                  shipping='9.99', shipping_taxable=shipping_taxable,
                  fees='4.50', fees_taxable=fees_taxable, increment='2.50')
    result = calculate(**kwargs)
    at_ceiling = calculate(**kwargs, current=result['max_hammer'])
    assert at_ceiling['savings_percent'] >= 70
    assert at_ceiling['current_total'] <= at_ceiling['budget']
    assert not calculate(**kwargs, current=result['max_hammer'] + Decimal('2.50'))['meets_discount']


def test_zero_retail_and_unaffordable_fees_are_not_eligible():
    assert not calculate(retail=0)['affordable']
    assert not calculate(retail=100, shipping=35)['affordable']
    assert calculate(retail=100, shipping=35)['max_hammer'] == 0


@pytest.mark.parametrize('kwargs', [{'increment': 0}, {'retail': -1}, {'retail': 'NaN'}, {'retail': 'Infinity'}, {'discount': 69}, {'auction_tax': 101}])
def test_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        calculate(**({'retail': 100} | kwargs))


def test_untaxed_premium_and_taxed_fees():
    result = calculate(retail=1000, current=100, premium=15, auction_tax=13,
                       premium_taxable=False, shipping=10, shipping_taxable=True,
                       fees=5, fees_taxable=True)
    assert result['current_total'] == Decimal('144.95')
