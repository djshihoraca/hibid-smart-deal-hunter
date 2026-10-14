import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dealhunter.security import hash_password
from dealhunter.storage import Store
from tests.test_integrations import image_bytes

APP = str(Path(__file__).parents[1] / 'app.py')
PASSWORD = 'test-only-long-password'


@pytest.fixture
def configured(monkeypatch, tmp_path):
    import streamlit as st
    st.cache_resource.clear()  # Each test is a new server; limiter sharing is tested separately.
    users = {'alice': hash_password(PASSWORD), 'bob': hash_password(PASSWORD)}
    monkeypatch.setenv('DEAL_HUNTER_USERS', json.dumps(users))
    monkeypatch.setenv('DEAL_HUNTER_DB', str(tmp_path / 'data.sqlite3'))
    monkeypatch.delenv('VISION_API_KEY', raising=False)
    monkeypatch.delenv('RETAIL_API_URL', raising=False)
    return tmp_path / 'data.sqlite3'


def widget(at, kind, label):
    return next(x for x in getattr(at, kind) if x.label == label)


def login(owner='alice'):
    at = AppTest.from_file(APP).run()
    assert not at.exception
    widget(at, 'text_input', 'App username').set_value(owner)
    widget(at, 'text_input', 'App password').set_value(PASSWORD)
    widget(at, 'button', 'Sign in').click().run()
    assert not at.exception
    assert 'auth' in at.session_state
    return at


def test_no_configuration_fails_closed(monkeypatch, tmp_path):
    db = tmp_path / 'not-created.sqlite3'
    monkeypatch.setenv('DEAL_HUNTER_USERS', '{}')
    monkeypatch.setenv('DEAL_HUNTER_DB', str(db))
    at = AppTest.from_file(APP).run()
    assert not at.exception and not db.exists()
    assert not at.number_input
    assert 'locked' in at.info[0].value


def test_wrong_password_and_logout_never_render_private_data(configured):
    at = AppTest.from_file(APP).run()
    widget(at, 'text_input', 'App username').set_value('bob')
    widget(at, 'text_input', 'App password').set_value('incorrect')
    widget(at, 'button', 'Sign in').click().run()
    assert not at.number_input and not configured.exists()
    at = login()
    assert [x.value for x in at.metric] == ['$113.00', '$26.00', '100.0%']
    widget(at, 'button', 'Sign out').click().run()
    assert not at.number_input and 'auth' not in at.session_state


def test_watchlist_survives_restart_and_is_private(configured):
    at = login()
    widget(at, 'text_input', 'Watchlist name').set_value('Workshop')
    widget(at, 'text_area', 'Brands (one per line)').set_value('Makita\nDeWalt')
    widget(at, 'text_area', 'Models (one per line)').set_value('ABC123\nDEF456')
    widget(at, 'text_area', 'Keywords (one per line)').set_value('drill\nbattery')
    widget(at, 'multiselect', 'Acceptable conditions').set_value(['Factory sealed', 'Used good'])
    widget(at, 'button', 'Save watchlist').click().run()
    assert not at.exception
    assert Store(configured).watchlists('alice')[0]['brands'] == ['Makita', 'DeWalt']
    reopened = login()
    assert 'Workshop' in widget(reopened, 'selectbox', 'Saved watchlist').options
    assert 'Workshop' not in widget(login('bob'), 'selectbox', 'Saved watchlist').options


def test_ceiling_approval_requires_separate_action_and_cannot_carry_forward(configured):
    at = login()
    widget(at, 'text_input', 'Exact item name / model').set_value('ABC123')
    widget(at, 'number_input', 'Proposed hammer bid (CAD)').set_value(30.0)
    at.run()
    assert any('Decision: Rejected' in e.value for e in at.error)
    button = widget(at, 'button', 'Approve this exception and save inspection')
    assert button.disabled
    widget(at, 'button', 'Save inspection').click().run()
    assert not Store(configured).history('alice')[0]['decision']['approved']
    widget(at, 'text_area', 'Reason for exceeding this bid ceiling').set_value('Accept lower savings for this specific bid')
    next(c for c in at.checkbox if c.label.startswith('I explicitly approve')).check()
    at.run()
    widget(at, 'button', 'Approve this exception and save inspection').click().run()
    saved = Store(configured).history('alice')[0]
    assert saved['decision']['approved'] and saved['decision']['status'] == 'requiring review'
    approval = saved.pop('approval')
    saved.pop('id')
    saved.pop('created_at')
    assert approval['scope_sha256'] == hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()
    widget(at, 'number_input', 'Proposed hammer bid (CAD)').set_value(31.0)
    at.run()
    assert not next(c for c in at.checkbox if c.label.startswith('I explicitly approve')).value
    widget(at, 'button', 'Save inspection').click().run()
    assert not Store(configured).history('alice')[0]['decision']['approved']
    assert Store(configured).history('bob') == []
    assert not at.exception


def test_verified_manual_price_and_photo_workflow(configured, monkeypatch):
    import streamlit as st
    raw = image_bytes()
    class Upload:
        name = 'photo.png'
        def getvalue(self):
            return raw
    monkeypatch.setattr(st, 'file_uploader', lambda *args, **kwargs: [Upload()])
    at = login()
    for label, value in [('HiBid listing URL', 'https://hibid.com/lot/123'), ('Exact item name / model', 'ABC123'),
                         ('Retail comparison URL (exact matching item)', 'https://retailer.example/abc'),
                         ('Canadian retailer name', 'Canadian store'), ('Retail model number', 'ABC123')]:
        widget(at, 'text_input', label).set_value(value)
    widget(at, 'multiselect', 'Claimed conditions').set_value(['Factory sealed'])
    widget(at, 'text_area', 'Full seller description').set_value('Original packaging with all included accessories')
    at.run()
    for label in ['I reviewed the uploaded photos manually',
                  'I checked this source: exact model, equivalent condition, in stock in Canada, price in CAD',
                  'I checked seller terms, tax treatment, pickup radius, exact model and equivalent condition',
                  'I accept the documented uncertainty; photos do not establish functionality']:
        widget(at, 'checkbox', label).check()
    at.run()
    assert any('Decision: Eligible' in e.value for e in at.success)
    assert any('not a live price' in e.value for e in at.info)
    widget(at, 'button', 'Save inspection').click().run()
    history = Store(configured).history('alice')
    assert history[0]['price_sources'][0]['kind'] == 'manual_verified'
    assert Store(configured).photos('alice', history[0]['id']) == [('photo.png', raw)]
    assert any('data:image/jpeg;base64,' in e.value for e in at.markdown)
    assert not at.get('imgs') and not at.get('download_button')
    widget(at, 'number_input', 'Comparable price (CAD, before tax)').set_value(200.0)
    at.run()
    assert not widget(at, 'checkbox', 'I checked this source: exact model, equivalent condition, in stock in Canada, price in CAD').value
    assert any('Decision: Requiring review' in e.value for e in at.warning)
    assert not at.exception


def test_multiple_sources_choose_lowest_delivered_and_persist_provenance(configured):
    at = login()
    widget(at, 'text_input', 'Exact item name / model').set_value('ABC123')
    widget(at, 'multiselect', 'Claimed conditions').set_value(['Factory sealed'])
    for label, value in [('Retail comparison URL (exact matching item)', 'https://one.example/product'),
                         ('Canadian retailer name', 'First store'), ('Retail model number', 'ABC123')]:
        widget(at, 'text_input', label).set_value(value)
    at.run()
    label = 'I checked this source: exact model, equivalent condition, in stock in Canada, price in CAD'
    widget(at, 'checkbox', label).check().run()
    widget(at, 'button', 'Add verified source to comparison').click().run()
    widget(at, 'text_input', 'Retail comparison URL (exact matching item)').set_value('https://two.example/product')
    widget(at, 'text_input', 'Canadian retailer name').set_value('Second store')
    widget(at, 'number_input', 'Comparable price (CAD, before tax)').set_value(90.0)
    widget(at, 'number_input', 'Retail delivery cost (CAD, tax included)').set_value(20.0)
    at.run()
    widget(at, 'checkbox', label).check().run()
    widget(at, 'button', 'Add verified source to comparison').click().run()
    widget(at, 'button', 'Save inspection').click().run()
    record = Store(configured).history('alice')[0]
    assert len(record['price_sources']) == 2
    assert record['selected_price_source']['retailer'] == 'First store'
    assert record['calculation']['retail_total'] == '113.000'
    assert not at.exception


@pytest.mark.parametrize('failure', [PermissionError('private details'), OSError('private details'), sqlite3.OperationalError('private details')])
def test_storage_failure_is_actionable_and_does_not_open_another_database(configured, monkeypatch, failure):
    attempts = []
    def unavailable(path):
        attempts.append(path)
        raise failure
    monkeypatch.setattr('dealhunter.storage.Store', unavailable)
    at = login()
    assert not at.exception
    assert not at.number_input
    assert len(attempts) == 1
    assert not configured.exists()
    assert 'Manage app' in at.error[0].value and 'DEAL_HUNTER_DB' in at.error[0].value
    assert 'private details' not in at.error[0].value
