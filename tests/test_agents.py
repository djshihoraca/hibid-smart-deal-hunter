from datetime import datetime, timedelta, timezone

import pytest

from dealhunter.agents import scope_hash, enqueue_team, run_next_job, BidAssistantAgent, ScoutAgent
from dealhunter.action_executor import execute_approved
from dealhunter.calculator import calculate
from dealhunter.market import research_market
from dealhunter.storage import Store

NOW = datetime.now(timezone.utc)


def comparable(i=1, **changes):
    return dict(platform='Facebook Marketplace', url=f'https://www.facebook.com/marketplace/item/{i}/',
                model='ABC123', condition='Used', currency='CAD', centre_postal='L7A 4X1', distance_km=10,
                status='sold', sale_verified=True, sale_evidence='User reviewed completed-sale receipt', sold_price=100+i,
                listed_at=(NOW-timedelta(days=12)).isoformat(), sold_at=(NOW-timedelta(days=2)).isoformat(),
                observed_at=(NOW-timedelta(days=1)).isoformat()) | changes


def report(records=None):
    return research_market(records if records is not None else [comparable(i) for i in range(1, 4)],
                           model='ABC123', conditions=['Used'], now=NOW)


def inspection():
    inputs = dict(retail=200, current=20)
    return dict(id=1, name='ABC123', listing_url='https://hibid.com/lot/123/example', conditions=['Used'],
                watchlist={'postal':'L7A 4X1','radius_km':50}, assessment={'flags':[]},
                bid_inputs=inputs, calculation={k: str(v) if not isinstance(v, bool) else v for k,v in calculate(**inputs).items()}, decision={'status':'eligible','reasons':[]})


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'private.sqlite3')


@pytest.mark.parametrize('changes', [
    {'model':'another'}, {'condition':'New'}, {'currency':'USD'}, {'distance_km':51},
    {'centre_postal':'M5V 1A1'}, {'sale_verified':False}, {'sale_evidence':''},
    {'status':'removed'}, {'sold_price':'NaN'}, {'distance_km':-1},
    {'observed_at':(NOW+timedelta(days=1)).isoformat()},
    {'observed_at':(NOW-timedelta(days=40)).isoformat()},
    {'sold_at':(NOW+timedelta(days=1)).isoformat()},
    {'url':'https://facebook.com.evil.test/marketplace/item/1'},
    {'url':'https://facebook.com/marketplace/'},
])
def test_market_excludes_invalid_evidence(changes):
    result = report([comparable(**changes)])
    assert result['verified_sold_sample'] == 0
    assert result['resale_price_cad'] is None
    assert result['excluded']


def test_market_price_days_and_dedup():
    result = report([comparable(i, sold_price=100*i) for i in range(1,4)] + [comparable(url='https://facebook.com/marketplace/item/1?tracking=abc')])
    assert result['resale_price_cad'] == {'low':150.0,'median':200.0,'high':250.0}
    assert result['historical_days_to_sell']['median'] == 10
    assert result['verified_sold_sample'] == 3
    assert result['confidence'] == 'limited'
    assert len(result['excluded']) == 1


def test_asking_prices_never_become_sold_estimates():
    result = report([comparable(i, status='active', asking_price=200) for i in range(1,4)])
    assert result['active_sample'] == 3
    assert result['resale_price_cad'] is None and result['historical_days_to_sell'] is None
    assert result['asking_price_snapshot_cad']['median'] == 200


def test_team_runs_durable_dependencies_and_keeps_accounts_private(store):
    ids = enqueue_team(store, 'alice', inspection(), [comparable(i) for i in range(1,4)],
                       asking_price=120, resale_costs_confirmed=True)
    assert store.claim_agent_job('bob') is None
    for _ in ids:
        assert run_next_job(store, 'alice') is not None
    assert run_next_job(store, 'alice') is None
    assert all(j['status'] == 'succeeded' for j in store.agent_jobs('alice'))
    assert len(store.agent_actions('alice')) == 3
    assert store.agent_actions('bob') == []
    assert store.agent_job('bob', ids[0]) is None
    # Drafting does not claim ownership, functionality, or publish anything.
    drafts = [a for a in store.agent_actions('alice') if a['kind']=='publish_ad']
    assert {a['payload']['platform'] for a in drafts} == {'Facebook Marketplace','Kijiji'}
    assert all(a['status']=='proposed' and not a['payload']['owned'] for a in drafts)


def test_failed_dependency_blocks_child_without_leaking_error(store):
    first = store.enqueue_agent_job('alice','Inspection',{})
    child = store.enqueue_agent_job('alice','Market research',{'depends_on':[first]})
    run_next_job(store,'alice')
    assert run_next_job(store,'alice') is None
    assert store.agent_job('alice', child)['status'] == 'blocked'
    assert 'KeyError' not in store.agent_jobs('alice')[-1]['error']


def test_bid_requires_market_margin_and_costs(store):
    context = {'store':store,'owner':'alice'}
    base = {'inspection':inspection(),'market_report':report()}
    assert 'blocked' in BidAssistantAgent().run(base,context)['status']
    assert 'blocked' in BidAssistantAgent().run(base | {'resale_costs_confirmed':True,'resale_costs':1000},context)['status']
    result = BidAssistantAgent().run(base | {'resale_costs_confirmed':True},context)
    assert result['status']=='awaiting exact-bid approval'
    assert result['details']['hammer_cad']=='20'
    assert store.agent_actions('alice')[0]['status']=='proposed'
    over = inspection(); over['bid_inputs']['current']=100
    assert 'blocked' in BidAssistantAgent().run(base | {'inspection':over,'resale_costs_confirmed':True},context)['status']


def action(store):
    result = BidAssistantAgent().run({'inspection':inspection(),'market_report':report(),'resale_costs_confirmed':True}, {'store':store,'owner':'alice'})
    identifier = result['action_id']
    return identifier, scope_hash(result['details'])


class FakeBid:
    kind='bid'
    permitted=True
    name='Test connector only'
    calls=0
    def prepare(self, payload):
        return {'scope_sha256':scope_hash(payload),'idempotency_supported':True,'currency':'CAD',
                'is_open':True,'hammer_cad':payload['hammer_cad'],'all_in_cad':payload['maximum_all_in_cad']}
    def submit(self, payload, idempotency_key):
        self.calls += 1
        return {'confirmed':True,'external_id':'test-123'}


def test_exact_approval_execution_and_duplicate_guard(store):
    identifier, fingerprint = action(store)
    adapter = FakeBid()
    with pytest.raises(ValueError): execute_approved(store,'alice',identifier,fingerprint,adapter)
    with pytest.raises(ValueError): store.approve_agent_action('bob',identifier,fingerprint,'yes')
    with pytest.raises(ValueError): store.approve_agent_action('alice',identifier,'changed','yes')
    store.approve_agent_action('alice',identifier,fingerprint,'Exact cost reviewed')
    with pytest.raises(ValueError): execute_approved(store,'alice',identifier,fingerprint)
    assert execute_approved(store,'alice',identifier,fingerprint,adapter)['external_id']=='test-123'
    with pytest.raises(ValueError): execute_approved(store,'alice',identifier,fingerprint,adapter)
    assert adapter.calls == 1
    assert store.agent_actions('alice')[0]['status']=='succeeded'


def test_unknown_submission_is_never_retried(store):
    identifier,fingerprint=action(store)
    adapter=FakeBid()
    def timeout(*args, **kwargs):
        adapter.calls += 1
        raise TimeoutError('secret provider body')
    adapter.submit=timeout
    store.approve_agent_action('alice',identifier,fingerprint,'yes')
    with pytest.raises(ValueError,match='unknown'): execute_approved(store,'alice',identifier,fingerprint,adapter)
    with pytest.raises(ValueError): execute_approved(store,'alice',identifier,fingerprint,adapter)
    assert adapter.calls==1 and store.agent_actions('alice')[0]['status']=='unknown'
    assert action(store)[0] == identifier


def test_changed_live_fees_stop_before_submission(store):
    identifier,fingerprint=action(store)
    adapter=FakeBid(); original=adapter.prepare
    adapter.prepare=lambda p: original(p) | {'all_in_cad':999}
    store.approve_agent_action('alice',identifier,fingerprint,'yes')
    with pytest.raises(ValueError): execute_approved(store,'alice',identifier,fingerprint,adapter)
    assert adapter.calls==0
    assert store.agent_actions('alice')[0]['status']=='approved'


def test_expiry_and_revocation(store):
    identifier,fingerprint=action(store)
    store.approve_agent_action('alice',identifier,fingerprint,'yes')
    with store.connect() as conn:
        conn.execute("UPDATE agent_actions SET expires_at='2000-01-01T00:00:00+00:00'")
    assert store.agent_actions('alice')[0]['status']=='expired'
    with pytest.raises(ValueError): execute_approved(store,'alice',identifier,fingerprint,FakeBid())
    new_id,_=action(store)
    store.revoke_agent_action('alice',new_id)
    assert store.agent_actions('alice')[0]['status']=='revoked'


def test_ad_approval_requires_ownership_and_authorized_assets(store):
    payload={'kind':'publish_ad','owned':False,'platform':'Kijiji'}
    identifier=store.propose_agent_action('alice',payload)
    with pytest.raises(ValueError): store.approve_agent_action('alice',identifier,scope_hash(payload),'yes')
    payload['owned']=True
    identifier=store.propose_agent_action('alice',payload)
    with pytest.raises(ValueError): store.approve_agent_action('alice',identifier,scope_hash(payload),'yes')


def test_budget_is_durable_and_per_owner(store):
    store.reserve_scout_calls('alice',7)
    with pytest.raises(ValueError): store.reserve_scout_calls('alice',4)
    store.reserve_scout_calls('alice',3)
    store.reserve_scout_calls('bob',10)
    with pytest.raises(ValueError): store.reserve_scout_calls('bob',1)


def test_scout_filters_and_queues_inspection(monkeypatch, store):
    lot = {'lot_id':'123','title':'Brand ABC123 drill','description':'missing charger',
           'currency':'CAD','is_closed':False,'url':'https://hibid.com/lot/123/drill'}
    calls=[]
    class ReadOnlyProvider:
        def __init__(self,key): assert key=='test-only'
        def search(self,**kwargs):
            calls.append(kwargs)
            return {'fetched_at':NOW.isoformat(),'results':[lot, lot,
                    lot | {'lot_id':'124','currency':'USD'}, lot | {'lot_id':'125','title':'Other product','description':''}]}
    monkeypatch.setattr('dealhunter.agents.ReefAPI',ReadOnlyProvider)
    result=ScoutAgent().run({'watchlist':{'models':['ABC123'],'brands':['Brand'],'keywords':['drill'],'postal':'L7A 4X1','radius_km':80}},
                            {'store':store,'owner':'alice','reef_key':'test-only'})
    assert len(result['candidates'])==1 and calls[0]['km']==50
    assert len(store.auction_snapshots('alice'))==1
    assert run_next_job(store,'alice') is not None
    review=store.agent_jobs('alice')[0]['result']
    assert review['status']=='requires manual verification'
    assert 'Photo contents' in review['limitations']


def test_tampered_payload_cannot_be_executed(store):
    identifier,fingerprint=action(store)
    store.approve_agent_action('alice',identifier,fingerprint,'yes')
    import json
    payload=store.agent_actions('alice')[0]['payload']; payload['hammer_cad']='999'
    with store.connect() as conn:
        conn.execute('UPDATE agent_actions SET payload=? WHERE id=?',(json.dumps(payload),identifier))
    adapter=FakeBid()
    with pytest.raises(ValueError): execute_approved(store,'alice',identifier,fingerprint,adapter)
    assert adapter.calls==0


def test_personal_ad_connector_and_photo_integrity(store):
    import hashlib
    from dealhunter.agents import AdWriterAgent
    photo=b'test-owned-photo'
    record=inspection() | {'photo_filenames':['own.jpg'],'photo_sha256':[hashlib.sha256(photo).hexdigest()]}
    record['id']=store.save_inspection('alice',record,[('own.jpg',photo)])
    result=AdWriterAgent().run({'inspection':record,'platform':'Kijiji','owned':True,'photos_authorized':True,'asking_price':120},
                             {'store':store,'owner':'alice'})
    identifier=result['action_id']; fingerprint=scope_hash(result['draft'])
    store.approve_agent_action('alice',identifier,fingerprint,'I own this item and these photos')
    adapter=FakeBid(); adapter.kind='publish_ad'; adapter.platform='Kijiji'; adapter.account_kind='business'
    with pytest.raises(ValueError,match='personal'): execute_approved(store,'alice',identifier,fingerprint,adapter)
    adapter.account_kind='personal'
    with store.connect() as conn:
        conn.execute("UPDATE photos SET content=?",(b'changed',))
    with pytest.raises(ValueError,match='photo'): execute_approved(store,'alice',identifier,fingerprint,adapter)
    assert adapter.calls==0
