import pytest

from dealhunter.catalogue import amount, catalogue_entries, percentage


def record(i, url='', name='Makita drill', decision='requiring review'):
    return {'id': i, 'listing_url': url, 'name': name, 'brand': 'Makita',
            'category': 'Power Tools', 'description': 'Includes battery and charger',
            'decision': {'status': decision}}


def test_browse_does_not_require_an_item_and_filters_latest_evidence():
    entries = [record(3, 'https://hibid.com/lot/1', decision='rejected'),
               record(2, 'https://hibid.com/lot/1', decision='eligible'),
               record(1)]
    assert [r['id'] for r in catalogue_entries(entries)] == [3, 1]
    assert catalogue_entries(entries, status='eligible') == []
    assert [r['id'] for r in catalogue_entries(entries, 'MAKITA battery', 'Power Tools')] == [3, 1]
    assert catalogue_entries(entries, category='Electronics') == []
    assert catalogue_entries(entries, query='missing brand') == []


def test_unknown_listing_urls_are_not_falsely_deduplicated():
    assert len(catalogue_entries([record(2), record(1)])) == 2


@pytest.mark.parametrize('value', [None, 'not available', 'NaN', 'Infinity'])
def test_missing_details_are_not_invented(value):
    assert amount(value) == 'Not recorded'
    assert percentage(value) == 'Not recorded'


def test_amounts_and_savings_format_without_precision_noise():
    assert amount('26') == 'CAD $26.00'
    assert percentage('70.000000001') == '70.0%'
