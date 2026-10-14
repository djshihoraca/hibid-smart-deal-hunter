import json
from unittest.mock import Mock

import pytest
import requests

from dealhunter.reefapi import (BASE_URL, ReefAPI, ReefAPIError, image_urls,
                               normalized_lot, numeric, radius_miles)

KEY = 'unit-test-reef-key'


class Response:
    def __init__(self, data=None, status=200, raw=None):
        self.status_code = status
        self.raw = raw if raw is not None else json.dumps({'ok': True, 'data': data, 'meta': {'credits': 1}}).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def iter_content(self, size):
        for i in range(0, len(self.raw), size):
            yield self.raw[i:i + size]


def lot(identifier=123, **changes):
    return {'lot_id': identifier, 'url': f'https://hibid.com/lot/{identifier}',
            'title': 'Makita drill ABC123', 'description': 'Untested, missing charger',
            'currency': 'CAD', 'current_bid': 10, 'next_bid_amount': 12.5,
            'bid_count': 3, 'is_closed': False, 'closes_at': '2026-12-01T18:00:00Z',
            'images': ['https://cdn.hibid.com/photo.jpg'],
            'auction': {'name': 'Tool auction', 'buyer_premium': '15%', 'terms': {'pickup': 'Collect in person'}},
            'auction_house': {'name': 'Example house', 'city': 'Brampton', 'state': 'ON', 'country': 'Canada'}, **changes}


def mock_calls(monkeypatch, *responses):
    post = Mock(side_effect=list(responses))
    monkeypatch.setattr('dealhunter.reefapi.requests.post', post)
    return post


def test_optional_query_can_browse_canada_and_auth_is_header_only(monkeypatch):
    post = mock_calls(monkeypatch, Response({'results': [lot()], 'total_available': 1, 'page_count': 1}))
    result = ReefAPI(KEY).search(postcode='L7A 4X1', km=50, province='ON')
    args, kwargs = post.call_args
    assert args[0] == BASE_URL + 'search'
    assert kwargs['headers'] == {'x-api-key': KEY}
    assert kwargs['json'] == {'country': 'Canada', 'status': 'open', 'archive': False,
                              'sort': 'ending_soonest', 'page': 1, 'max_results': 10,
                              'zip': 'L7A 4X1', 'radius_miles': 31, 'state': 'ON'}
    assert not kwargs['allow_redirects'] and kwargs['stream']
    assert kwargs.get('verify', True) is True
    assert result['results'][0]['current_bid'] == '10'
    assert KEY not in json.dumps(result)


def test_multi_category_search_is_bounded_and_deduplicated(monkeypatch):
    post = mock_calls(monkeypatch,
                      Response({'results': [lot(1)], 'total_available': 10}),
                      Response({'results': [lot(1), lot(2)], 'total_available': 20}))
    result = ReefAPI(KEY).search(category_ids=[10, 20, 10], query='drill', page=2, sort='newest')
    assert [row['lot_id'] for row in result['results']] == ['1', '2']
    assert [c.kwargs['json']['category_id'] for c in post.call_args_list] == [10, 20]
    assert result['page'] == 2 and len(result['reports']) == 2


def test_detail_returns_complete_terms_without_bidder_ladder(monkeypatch):
    post = mock_calls(monkeypatch, Response(lot(123, pictures=[{'url': 'https://cdn.hibid.com/full.jpg'}], category_path=['Tools'])))
    result = ReefAPI(KEY).detail('123')
    assert post.call_args.args[0] == BASE_URL + 'detail'
    assert post.call_args.kwargs['json'] == {'lot': '123', 'include_bids': False}
    assert result['lot']['auction']['terms']['pickup'] == 'Collect in person'
    assert len(image_urls(result['lot'])) == 2


def test_details_for_another_lot_are_not_attached(monkeypatch):
    mock_calls(monkeypatch, Response(lot(999)))
    with pytest.raises(ReefAPIError, match='different lot'):
        ReefAPI(KEY).detail('123')


def test_nested_category_tree_uses_real_provider_ids(monkeypatch):
    mock_calls(monkeypatch, Response({'categories': [{'id': 10, 'name': 'Tools', 'children': [
        {'id': 11, 'name': 'Drills', 'full_name': 'Tools / Drills', 'children': []}]}]}))
    result = ReefAPI(KEY).categories()
    assert result['categories'] == {10: 'Tools', 11: 'Tools / Drills'}


@pytest.mark.parametrize('status', [301, 302, 401, 403, 429, 500])
def test_http_failures_are_not_retried_or_relabelled_as_empty_results(monkeypatch, status):
    post = mock_calls(monkeypatch, Response({}, status=status))
    with pytest.raises(ReefAPIError):
        ReefAPI(KEY).search()
    assert post.call_count == 1


@pytest.mark.parametrize('raw', [b'not json', b'[]', b'{"ok":false,"error":{"message":"private key"}}',
                                  b'{"ok":true,"data":[]}', b'{"ok":true,"data":{"results":null}}'])
def test_bad_envelopes_fail_without_provider_error_leaks(monkeypatch, raw):
    mock_calls(monkeypatch, Response(raw=raw))
    with pytest.raises(ReefAPIError) as failure:
        ReefAPI(KEY).search()
    assert 'private key' not in str(failure.value)


def test_bounded_response_and_connection_errors_do_not_expose_credentials(monkeypatch):
    monkeypatch.setattr('dealhunter.reefapi.MAX_RESPONSE', 100)
    mock_calls(monkeypatch, Response(raw=b'x' * 101))
    with pytest.raises(ReefAPIError, match='size'):
        ReefAPI(KEY).search()
    monkeypatch.setattr('dealhunter.reefapi.requests.post', Mock(side_effect=requests.ConnectionError(KEY)))
    with pytest.raises(ReefAPIError) as failure:
        ReefAPI(KEY).search()
    assert KEY not in str(failure.value)


def test_credentials_are_removed_before_snapshots_or_ui(monkeypatch):
    record = lot(123, description='description echoed ' + KEY,
                 auction={'terms': {'api_key': KEY, 'authorization': KEY, 'pickup': 'Private testing'}})
    mock_calls(monkeypatch, Response({'results': [record], 'api_key': KEY}))
    result = ReefAPI(KEY).search()
    encoded = json.dumps(result)
    assert KEY not in encoded and 'authorization' not in encoded and 'api_key' not in encoded
    assert '[redacted]' in encoded


@pytest.mark.parametrize('kwargs', [{'province': 'Ontario'}, {'category_ids': [True]}, {'category_ids': list(range(1, 7))},
                                    {'page': 0}, {'max_results': 101}, {'sort': 'random'}, {'status': 'invalid'},
                                    {'page': 101, 'max_results': 100}, {'postcode': 'L7A', 'km': 0}])
def test_invalid_search_parameters_do_not_spend_credits(monkeypatch, kwargs):
    post = mock_calls(monkeypatch)
    with pytest.raises(ReefAPIError):
        ReefAPI(KEY).search(**kwargs)
    post.assert_not_called()


def test_unavailable_bid_is_not_zero_and_currency_is_not_converted():
    result = normalized_lot(lot(current_bid='NaN', next_bid_amount=-1, currency='USD'))
    assert result['current_bid'] is None and result['next_bid_amount'] is None
    assert result['currency'] == 'USD'
    assert numeric(True) is None
    assert radius_miles(50) == 31


def test_image_urls_do_not_allow_private_or_active_content():
    urls = image_urls({'images': ['https://cdn.hibid.com/image.jpg', 'http://unsafe.example/a',
                                 'javascript:alert(1)', 'https://127.0.0.1/a', 'https://localhost/a',
                                 'https://user:password@cdn.hibid.com/a']})
    assert urls == ['https://cdn.hibid.com/image.jpg']


def test_fixed_read_only_actions_do_not_accept_login_or_bidding(monkeypatch):
    post = mock_calls(monkeypatch)
    with pytest.raises(ReefAPIError):
        ReefAPI(KEY)._call('bid', {})
    with pytest.raises(ReefAPIError):
        ReefAPI('')
    post.assert_not_called()
