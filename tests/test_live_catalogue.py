import json
from unittest.mock import Mock

import pytest

from dealhunter.reefapi import review_imported_bid
from dealhunter.storage import Store
from tests.test_app import configured, login, widget
from tests.test_reefapi import KEY, Response, lot


def api_search(**changes):
    return Response({'results': [lot(**changes)], 'total_available': 1, 'page_count': 1})


def bind(monkeypatch, *responses):
    monkeypatch.setenv('REEFAPI_KEY', KEY)
    post = Mock(side_effect=list(responses))
    monkeypatch.setattr('dealhunter.reefapi.requests.post', post)
    return post


def test_no_api_calls_before_explicit_search_and_changed_filters_hide_old_results(configured, monkeypatch):
    post = bind(monkeypatch, api_search())
    at = login()
    post.assert_not_called()
    assert widget(at, 'radio', 'Catalogue source').value == 'Live HiBid listings'
    assert widget(at, 'text_input', 'Live search (optional)').value == ''
    widget(at, 'button', 'Search / refresh current listings').click().run()
    assert post.call_count == 1 and not at.exception
    assert any(h.value == 'Makita drill ABC123' for h in at.subheader)
    assert any('Current bid at fetch: CAD 10.00' in t.value for t in at.text)
    assert any('referrerpolicy="no-referrer"' in m.value for m in at.markdown)
    assert KEY not in '\n'.join(m.value for m in at.markdown)
    widget(at, 'text_input', 'Live search (optional)').set_value('another model').run()
    assert not any(h.value == 'Makita drill ABC123' for h in at.subheader)
    assert post.call_count == 1


def test_detail_save_and_inspect_preserve_provider_metadata_without_credentials(configured, monkeypatch):
    detailed = lot(next_bid_amount=15, pictures=[{'url': 'https://cdn.hibid.com/full.jpg'}],
                   auction={'name': 'Tool auction', 'terms': {'buyer_premium': 'Tiered: read auction terms', 'api_key': KEY}})
    post = bind(monkeypatch, api_search(), Response(detailed))
    at = login()
    widget(at, 'button', 'Search / refresh current listings').click().run()
    widget(at, 'button', 'Fetch full lot details (1 credit)').click().run()
    assert not at.exception and post.call_count == 2
    assert post.call_args.kwargs['json'] == {'lot': '123', 'include_bids': False}
    widget(at, 'button', 'Save auction snapshot').click().run()
    records = Store(configured).auction_snapshots('alice')
    assert len(records) == 1
    assert records[0]['lot']['auction']['terms']['buyer_premium'] == 'Tiered: read auction terms'
    assert KEY not in json.dumps(records)
    assert Store(configured).auction_snapshots('bob') == []
    widget(at, 'button', 'Inspect this lot').click().run()
    assert not at.exception
    assert widget(at, 'text_input', 'HiBid listing URL').value == 'https://hibid.com/lot/123'
    assert widget(at, 'text_input', 'Exact item name / model').value == 'Makita drill ABC123'
    assert widget(at, 'number_input', 'Proposed hammer bid (CAD)').value == 15
    assert widget(at, 'multiselect', 'Claimed conditions').value == []
    assert not widget(at, 'checkbox', 'I checked seller terms, tax treatment, pickup radius, exact model and equivalent condition').value
    widget(at, 'button', 'Save inspection').click().run()
    assert Store(configured).history('alice')[0]['auction_source']['lot']['lot_id'] == '123'
    assert not Store(configured).history('alice')[0]['decision']['approved']
    assert post.call_count == 2
    widget(at, 'radio', 'Catalogue source').set_value('Saved auction snapshots').run()
    assert not at.exception and any(h.value == 'Makita drill ABC123' for h in at.subheader)
    assert post.call_count == 2


@pytest.mark.parametrize('changes', [{'currency': 'USD'}, {'is_closed': True}])
def test_non_cad_and_closed_lots_cannot_be_imported(configured, monkeypatch, changes):
    bind(monkeypatch, api_search(**changes))
    at = login()
    widget(at, 'button', 'Search / refresh current listings').click().run()
    assert widget(at, 'button', 'Inspect this lot').disabled
    assert not at.exception


def test_failed_refresh_does_not_show_an_old_result_as_current(configured, monkeypatch):
    post = bind(monkeypatch, api_search(), Response({}, status=429))
    at = login()
    widget(at, 'button', 'Search / refresh current listings').click().run()
    assert any(h.value == 'Makita drill ABC123' for h in at.subheader)
    widget(at, 'button', 'Search / refresh current listings').click().run()
    assert not any(h.value == 'Makita drill ABC123' for h in at.subheader)
    assert any('rate limit' in e.value for e in at.error)
    assert post.call_count == 2 and not at.exception


def test_categories_and_multiple_category_requests_require_user_actions(configured, monkeypatch):
    post = bind(monkeypatch, Response({'categories': [{'id': 10, 'name': 'Tools'}, {'id': 20, 'name': 'Electronics'}]}),
                api_search(), api_search())
    at = login()
    post.assert_not_called()
    widget(at, 'button', 'Load HiBid categories (1 credit)').click().run()
    widget(at, 'multiselect', 'HiBid categories (optional, up to 5)').set_value([10, 20]).run()
    assert post.call_count == 1
    widget(at, 'button', 'Search / refresh current listings').click().run()
    assert post.call_count == 3
    assert [c.kwargs['json']['category_id'] for c in post.call_args_list[1:]] == [10, 20]
    assert len([h for h in at.subheader if h.value == 'Makita drill ABC123']) == 1
    assert not at.exception


def test_missing_key_is_an_actionable_configuration_message(configured, monkeypatch):
    post = Mock()
    monkeypatch.setattr('dealhunter.reefapi.requests.post', post)
    at = login()
    widget(at, 'radio', 'Catalogue source').set_value('Live HiBid listings').run()
    assert any('REEFAPI_KEY' in i.value for i in at.info)
    post.assert_not_called()
    assert not at.exception


@pytest.mark.parametrize('source,proposed', [({'currency': 'USD'}, 15), ({'is_closed': True}, 15), ({'next_bid_amount': 15}, 10)])
def test_inspection_guard_rejects_invalid_imported_proposals_even_with_override(source, proposed):
    decision = {'status': 'eligible', 'reasons': [], 'approved': True}
    guarded = review_imported_bid(decision, {'lot': lot(**source)}, proposed)
    assert guarded['status'] == 'rejected' and not guarded['approved']


def test_closing_time_is_localized_without_guessing_missing_timezone():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from dealhunter.live_catalogue import closing_time
    formatter = lambda v: datetime.fromisoformat(v.replace('Z', '+00:00')).astimezone(ZoneInfo('America/Toronto')).strftime('%H:%M %Z')
    assert closing_time('2026-12-01T18:00:00Z', formatter) == '13:00 EST'
    assert 'Timezone not reported' in closing_time('2026-12-01T18:00:00', formatter)
    assert 'Not reported' in closing_time(None, formatter)
