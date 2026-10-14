import pytest

from dealhunter.calculator import calculate
from dealhunter.inspection import assess, decide
from dealhunter.security import LoginLimiter, hash_password, verify_password
from dealhunter.storage import Store


def clean():
    return assess('Original packaging and included accessories', ['Factory sealed'], 1, {'reviewed': True})


def decision(current=0, approved=False, **kwargs):
    return decide(calculate(retail=100, current=current), kwargs.pop('assessment', clean()),
                  kwargs.pop('source', {'kind': 'manual_verified', 'verified': True}),
                  complete=True, accepted_conditions=['Factory sealed'],
                  claimed_conditions=['Factory sealed'], approved=approved, **kwargs)


def test_eligible_manual_verified_is_distinguished_from_unverified():
    assert decision()['status'] == 'eligible'
    assert decision(source={'verified': False})['status'] == 'requiring review'


def test_damage_missing_and_inconsistent_claims():
    assessment = assess('Open box, damaged shell, missing charger, untested', ['Factory sealed'], 1, {'reviewed': True})
    assert assessment['description_damage'] and assessment['missing_accessories'] and assessment['condition_conflict']
    assert 'Unknown' in assessment['functionality']
    assert decision(assessment=assessment)['status'] == 'requiring review'


def test_ai_observation_never_becomes_functionality_proof():
    assessment = assess('New item', ['Factory sealed'], 1, {'reviewed': True}, {'damage': [], 'uncertainty': ['Cannot see accessories']})
    assert decision(assessment=assessment)['status'] == 'requiring review'
    assert 'never proof' in assessment['functionality']


def test_explicit_override_cannot_become_eligible_or_survive_new_decision():
    assert decision(current=30)['status'] == 'rejected'
    approved = decision(current=30, approved=True)
    assert approved['approved'] and approved['status'] == 'requiring review'
    assert not decision(current=31)['approved']


def test_above_rounded_ceiling_always_needs_explicit_approval():
    assert calculate(retail=100, current='26.01')['meets_discount']
    assert decision(current='26.01')['status'] == 'rejected'
    assert decision(current='26.01', approved=True)['approved']


def test_uncertainty_in_freeform_notes_requires_review():
    assessment = assess('Original packaging', ['Factory sealed'], 1,
                        {'reviewed': True, 'notes': 'uncertain if the charger is missing'})
    assert decision(assessment=assessment)['status'] == 'requiring review'


def test_conditions_outside_watchlist_cannot_be_overridden():
    result = decide(calculate(retail=100, current=30), clean(), {'verified': True}, complete=True,
                    accepted_conditions=['Factory sealed'], claimed_conditions=['For parts / not working'], approved=True)
    assert result['status'] == 'rejected' and not result['approved']


def test_durable_user_isolation_and_photos(tmp_path):
    path = tmp_path / 'private.sqlite3'
    store = Store(path)
    store.save_watchlist('alice', 'tools', {'models': ['ABC'], 'conditions': ['Used good', 'Like new']})
    store.save_watchlist('bob', 'tools', {'models': ['DEF']})
    record_id = store.save_inspection('alice', {'name': 'ABC', 'decision': {'status': 'requiring review'}}, [('photo.png', b'photo bytes')])
    reopened = Store(path)
    assert reopened.watchlists('alice')[0]['models'] == ['ABC']
    assert reopened.watchlists('bob')[0]['models'] == ['DEF']
    assert reopened.history('bob') == []
    assert reopened.photos('bob', record_id) == []
    assert reopened.photos('alice', record_id) == [('photo.png', b'photo bytes')]
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        reopened.history('')


def test_watchlist_upsert_and_parameterized_usernames(tmp_path):
    store = Store(tmp_path / 'db')
    owner = "alice' OR 1=1 --"
    store.save_watchlist(owner, 'one', {'keywords': ['first']})
    store.save_watchlist(owner, 'one', {'keywords': ['second']})
    assert len(store.watchlists(owner)) == 1
    assert store.watchlists('alice') == []


def test_password_hashes_are_salted_and_fail_closed():
    value = hash_password('a unique long app password')
    assert verify_password('a unique long app password', value)
    assert not verify_password('incorrect', value)
    assert not verify_password('anything', 'bad configuration')
    assert value != hash_password('a unique long app password')
    with pytest.raises(ValueError):
        hash_password('short')


def test_login_throttle_is_shared_between_sessions():
    limiter = LoginLimiter()
    assert all(limiter.allow('alice') for _ in range(5))
    assert not limiter.allow('alice')
    assert limiter.allow('bob')
