import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from PIL import Image

from dealhunter.integrations import https_url, live_retail, source_fresh, validate_photo, vision_photos


def image_bytes():
    output = io.BytesIO()
    Image.new('RGB', (30, 20), 'white').save(output, format='PNG')
    return output.getvalue()


def source():
    return {'model': 'ABC123', 'condition': 'Like new', 'country': 'CA', 'currency': 'CAD',
            'in_stock': True, 'price': 199.99, 'delivery': 5, 'url': 'https://retailer.example/product',
            'retailer': 'Canadian store', 'observed_at': datetime.now(timezone.utc).isoformat()}


def mock_post(monkeypatch, payload, status=200):
    response = Mock(status_code=status, content=b'{}')
    response.json.return_value = payload
    post = Mock(return_value=response)
    monkeypatch.setattr('dealhunter.integrations.requests.post', post)
    return post


def test_live_price_is_exact_available_cad_and_fresh(monkeypatch):
    payload = source() | {'api_key': 'must never persist'}
    post = mock_post(monkeypatch, payload)
    result = live_retail('https://adapter.example/price', 'test-key', model='ABC123', condition='Like new')
    assert result['verified'] and result['kind'] == 'live_verified'
    assert 'api_key' not in result and source_fresh(result)
    assert post.call_args.kwargs['allow_redirects'] is False
    assert post.call_args.args[0] == 'https://adapter.example/price'


@pytest.mark.parametrize('changes', [
    {'model': 'different'}, {'condition': 'Refurbished'}, {'country': 'US'}, {'currency': 'USD'},
    {'in_stock': False}, {'price': 0}, {'price': 'NaN'}, {'delivery': -1}, {'url': 'http://store.example'},
    {'observed_at': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
    {'observed_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
])
def test_invalid_sources_never_verified(monkeypatch, changes):
    mock_post(monkeypatch, source() | changes)
    with pytest.raises(ValueError):
        live_retail('https://adapter.example', '', model='ABC123', condition='Like new')


def test_redirect_is_not_followed(monkeypatch):
    post = mock_post(monkeypatch, {}, status=302)
    with pytest.raises(ValueError):
        live_retail('https://adapter.example', '', model='ABC123', condition='Like new')
    assert not post.call_args.kwargs['allow_redirects']


def test_expired_source_and_unsafe_url():
    assert not source_fresh(source() | {'kind': 'live_verified', 'observed_at': '2000-01-01T00:00:00+00:00'})
    assert not https_url('https://user:password@retailer.example/')
    assert not https_url('javascript:alert(1)')
    assert not https_url('https://host:bad')


def test_photo_validation_and_vision_response(monkeypatch):
    raw = image_bytes()
    validate_photo(raw)
    with pytest.raises(ValueError):
        validate_photo(b'not an image')
    with pytest.raises(ValueError):
        validate_photo(b'x' * (10 * 1024 * 1024 + 1))
    assessment = {'damage': ['Cracked corner'], 'missing_accessories': [], 'inconsistencies': [], 'uncertainty': ['Cannot test power']}
    post = mock_post(monkeypatch, {'choices': [{'message': {'content': json.dumps(assessment)}}]})
    assert vision_photos([('photo.png', raw)], 'Untested', ['Used good'], 'test-key') == assessment
    assert 'Never infer functionality' in post.call_args.kwargs['json']['messages'][0]['content'][0]['text']
    assert not post.call_args.kwargs['allow_redirects']


def test_bad_vision_schema_fails(monkeypatch):
    mock_post(monkeypatch, {'choices': [{'message': {'content': '{"damage": "works perfectly"}'}}]})
    with pytest.raises(ValueError):
        vision_photos([('photo.png', image_bytes())], 'description', [], 'test-key')
