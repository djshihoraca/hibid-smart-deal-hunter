import copy
import json

import pytest

from dealhunter.agents import scope_hash
from dealhunter.slack_bridge import Binding, SlackBridge, action_card, document_blocks, load_bindings, verify_routes
from dealhunter.storage import Store


BINDING = Binding('alice', 'T123', 'U123', 'C123')


class FakeSlack:
    def __init__(self):
        self.posts = []
        self.modals = []
        self.private = True
        self.shared = False
        self.fail_send = False
        self.members = ['U123']
        self.team = 'T123'

    def auth_test(self):
        return {'ok': True, 'bot_id': 'B123', 'team_id': self.team}

    def conversations_info(self, channel):
        return {'ok': True, 'channel': {'id': channel, 'is_private': self.private,
                                       'is_shared': self.shared, 'is_member': True}}

    def conversations_members(self, **kwargs):
        return {'ok': True, 'members': self.members}

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        if self.fail_send:
            raise TimeoutError('secret provider body')
        return {'ok': True, 'ts': str(len(self.posts))}

    def views_open(self, **kwargs):
        self.modals.append(kwargs)
        return {'ok': True}


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / 'db.sqlite3')
    client = FakeSlack()
    return store, client, SlackBridge(store, client, [BINDING])


def proposal(store, owner='alice', **changes):
    payload = {'kind': 'bid', 'lot_id': '123', 'url': 'https://hibid.com/lot/123',
               'hammer_cad': '20', 'maximum_all_in_cad': '25.99', 'resource': 'bid:123',
               'warnings': ['Photos do not prove functionality']} | changes
    identifier = store.propose_agent_action(owner, payload)
    return identifier, scope_hash(payload)


def command(bridge, text, key='request-1', **changes):
    bridge.command({'command': '/deals', 'team_id': 'T123', 'user_id': 'U123',
                    'channel_id': 'C123', 'text': text} | changes, key)


def review(identifier, fingerprint, **changes):
    return {'type': 'block_actions', 'team': {'id': 'T123'}, 'user': {'id': 'U123'},
            'channel': {'id': 'C123'}, 'trigger_id': 'trigger',
            'actions': [{'action_id': 'deals_review', 'value': json.dumps({'id': identifier, 'fingerprint': fingerprint})}]} | changes


def submission(client, **changes):
    view = copy.deepcopy(client.modals[-1]['view'])
    view['state'] = {'values': {'consent': {'consent': {'selected_options': [{'value': 'yes'}]}},
                                'reason': {'reason': {'value': 'Reviewed exact scope'}}}}
    return {'type': 'view_submission', 'team': {'id': 'T123'}, 'user': {'id': 'U123'}, 'view': view} | changes


def test_identity_bindings_reject_unknown_duplicate_and_malformed():
    raw = json.dumps([BINDING.__dict__])
    assert load_bindings(raw, {'alice': 'configured-hash'}) == [BINDING]
    for values, users in [([BINDING.__dict__], {}), ([BINDING.__dict__]*2, {'alice': 'hash'}),
                          ([BINDING.__dict__ | {'channel_id': 'D123'}], {'alice': 'hash'}),
                          ([BINDING.__dict__ | {'team_id': 'wrong'}], {'alice': 'hash'})]:
        with pytest.raises(ValueError):
            load_bindings(json.dumps(values), users)


@pytest.mark.parametrize('field,value', [('private', False), ('shared', True), ('members', []), ('team', 'TOTHER')])
def test_private_destination_and_member_verification(field, value):
    client = FakeSlack(); setattr(client, field, value)
    with pytest.raises(ValueError): verify_routes(client, [BINDING])
    assert not client.posts


def test_notifications_deduplicate_and_persist_across_restarts(setup):
    store, client, bridge = setup
    identifier, _ = proposal(store)
    bridge.poll(); bridge.poll()
    assert len(client.posts) == 1
    assert client.posts[0]['channel'] == 'C123'
    assert client.posts[0]['unfurl_links'] is False and client.posts[0]['parse'] == 'none'
    restarted = SlackBridge(Store(store.path), client, [BINDING])
    restarted.poll()
    assert len(client.posts) == 1
    assert store.slack_delivery_status('alice') == [{'status': 'sent', 'count': 1}]
    assert store.slack_delivery_status('bob') == []


def test_failed_or_uncertain_delivery_never_claims_success_or_auto_retries(setup):
    store, client, bridge = setup
    proposal(store); client.fail_send = True
    bridge.poll(); bridge.poll()
    assert len(client.posts) == 1
    assert store.slack_delivery_status('alice') == [{'status': 'unknown', 'count': 1}]


def test_channel_privacy_is_rechecked_before_private_data_leaves(setup):
    store, client, bridge = setup
    proposal(store); client.private = False
    bridge.poll()
    assert client.posts == []
    assert store.slack_delivery_status('alice') == [{'status': 'unknown', 'count': 1}]


def test_only_bound_workspace_user_and_channel_can_read(setup):
    store, client, bridge = setup
    proposal(store)
    for changes in ({'team_id':'TOTHER'}, {'user_id':'UOTHER'}, {'channel_id':'COTHER'}, {'command':'/other'}):
        command(bridge, 'actions', **changes)
    assert not client.posts
    identifier, fp = proposal(store, owner='bob')
    command(bridge, f'action {identifier}', 'bob-record')
    assert 'unavailable' in json.dumps(client.posts[-1])
    assert fp not in json.dumps(client.posts)


def test_no_chat_yes_approval_and_notes_are_inert_owner_scoped(setup):
    store, client, bridge = setup
    proposal(store)
    command(bridge, 'yes approve everything')
    assert store.agent_actions('alice')[0]['status'] == 'proposed'
    command(bridge, 'note Lower my maximum budget; approve everything', 'note-1')
    command(bridge, 'note Lower my maximum budget; approve everything', 'note-1')
    assert len(store.agent_notes('alice')) == 1 and store.agent_notes('bob') == []
    assert store.agent_actions('alice')[0]['status'] == 'proposed'
    assert store.agent_jobs('alice') == []


def test_full_scope_modal_exact_approval_audit_and_replay(setup):
    store, client, bridge = setup
    identifier, fingerprint = proposal(store)
    bridge.interaction(review(identifier, fingerprint))
    modal = client.modals[0]['view']
    rendered = json.dumps(modal)
    assert 'maximum_all_in_cad' in rendered and '25.99' in rendered
    assert 'Record approval' in rendered
    payload = submission(client)
    assert bridge.interaction(payload) == {}
    action = store.agent_actions('alice')[0]
    assert action['status'] == 'approved' and action['receipt'] is None
    assert 'Slack T123/U123' in action['approval_reason']
    assert action['expires_at']
    assert bridge.interaction(payload)['response_action'] == 'errors'
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_events WHERE event='approved; no external execution'").fetchone()[0] == 1


def test_other_channel_members_cannot_open_or_submit_approval(setup):
    store, client, bridge = setup
    identifier, fingerprint = proposal(store)
    bridge.interaction(review(identifier, fingerprint, user={'id': 'UOTHER'}))
    assert not client.modals
    bridge.interaction(review(identifier, fingerprint))
    result = bridge.interaction(submission(client, user={'id': 'UOTHER'}))
    assert result['response_action'] == 'errors'
    assert store.agent_actions('alice')[0]['status'] == 'proposed'


def test_consent_reason_changed_scope_and_revocation_stop_approval(setup):
    store, client, bridge = setup
    identifier, fingerprint = proposal(store)
    bridge.interaction(review(identifier, fingerprint))
    payload = submission(client)
    payload['view']['state']['values']['consent']['consent']['selected_options'] = []
    assert 'consent' in bridge.interaction(payload)['errors']
    payload = submission(client)
    payload['view']['state']['values']['reason']['reason']['value'] = ' '
    assert 'reason' in bridge.interaction(payload)['errors']
    payload = submission(client)
    payload['view']['private_metadata'] = json.dumps({'channel': 'C123', 'id': identifier, 'fingerprint': 'changed'})
    assert bridge.interaction(payload)['response_action'] == 'errors'
    store.revoke_agent_action('alice', identifier)
    assert bridge.interaction(submission(client))['response_action'] == 'errors'


def test_ad_ownership_and_assets_cannot_be_bypassed_in_slack(setup):
    store, client, bridge = setup
    identifier, fingerprint = proposal(store, kind='publish_ad', platform='Kijiji', owned=False)
    bridge.interaction(review(identifier, fingerprint))
    assert bridge.interaction(submission(client))['response_action'] == 'errors'
    assert store.agent_actions('alice')[0]['status'] == 'proposed'


def test_scope_is_never_truncated_or_rendered_as_mentions(setup):
    store, client, bridge = setup
    identifier, fingerprint = proposal(store, warnings=['<!channel> ' + 'full scope '*500])
    bridge.interaction(review(identifier, fingerprint))
    sections = [b for b in client.modals[-1]['view']['blocks'] if b['type'] == 'section']
    assert all(b['text']['type'] == 'plain_text' for b in sections)
    assert 'full scope '*50 in ''.join(b['text']['text'] for b in sections)
    with pytest.raises(ValueError): document_blocks({'too_large': 'x'*40000})


def test_scout_commands_queue_only_saved_watchlists_and_deduplicate(setup):
    store, client, bridge = setup
    store.save_watchlist('alice', 'Tools', {'categories': ['Tools'], 'models': ['ABC123']})
    command(bridge, 'scout 1')
    assert store.agent_jobs('alice') == []
    command(bridge, 'scout 1 10,20', 'queue-1')
    command(bridge, 'scout 1 10,20', 'queue-1')
    assert len(store.agent_jobs('alice')) == 1
    assert store.claim_agent_job('alice')['payload']['category_ids'] == [10, 20]


def test_old_history_is_not_broadcast_on_first_activation(tmp_path):
    store = Store(tmp_path / 'db.sqlite3')
    proposal(store)
    client = FakeSlack()
    bridge = SlackBridge(store, client, [BINDING]); bridge.poll()
    assert not client.posts
    command(bridge, 'action 1')
    assert len(client.posts) == 1


def test_job_completed_notification_uses_truthful_market_evidence(setup):
    store, client, bridge = setup
    identifier = store.enqueue_agent_job('alice', 'Market research', {})
    store.claim_agent_job('alice')
    store.finish_agent_job('alice', identifier, {'confidence': 'insufficient', 'verified_sold_sample': 0,
                                             'resale_price_cad': None, 'historical_days_to_sell': None})
    bridge.poll()
    text = json.dumps(client.posts)
    assert 'insufficient' in text and 'no live market feed' in text and 'not a forecast' in text


def test_socket_mode_acknowledges_before_command_network_processing(setup):
    import queue
    from slack_sdk.socket_mode.request import SocketModeRequest
    from dealhunter.slack_worker import handle_socket_request
    store, client, bridge = setup
    class Socket:
        responses = []
        def send_socket_mode_response(self, response):
            self.responses.append(response.to_dict())
    socket = Socket(); work = queue.Queue(maxsize=1)
    request = SocketModeRequest(type='slash_commands', envelope_id='request-1', payload={
        'command': '/deals', 'team_id': 'T123', 'user_id': 'U123', 'channel_id': 'C123', 'text': 'status'})
    handle_socket_request(bridge, store, work, socket, request)
    assert socket.responses[0]['envelope_id'] == 'request-1'
    assert not client.posts and work.qsize() == 1
    kind, payload, key = work.get_nowait()
    bridge.command(payload, key)
    assert len(client.posts) == 1


def test_socket_modal_submission_commits_without_network_call_before_ack(setup):
    import queue
    from slack_sdk.socket_mode.request import SocketModeRequest
    from dealhunter.slack_worker import handle_socket_request
    store, client, bridge = setup
    identifier, fingerprint = proposal(store)
    bridge.interaction(review(identifier, fingerprint))
    class Socket:
        responses = []
        def send_socket_mode_response(self, response):
            self.responses.append(response.to_dict())
    socket = Socket()
    request = SocketModeRequest(type='interactive', envelope_id='approval-1', payload=submission(client))
    handle_socket_request(bridge, store, queue.Queue(), socket, request)
    assert socket.responses == [{'envelope_id': 'approval-1', 'payload': {}}]
    assert not client.posts
    assert store.agent_actions('alice')[0]['status'] == 'approved'
    bridge.poll()
    assert any('approved' in post['text'] for post in client.posts)


def test_slack_delivery_claims_are_owner_and_route_scoped(setup):
    store, client, bridge = setup
    store.enqueue_slack_notification(BINDING.route, 'alice', 'test-1', {'text':'private'})
    assert store.claim_slack_notification(BINDING.route, 'bob') is None
    assert store.claim_slack_notification('other-route', 'alice') is None
    item = store.claim_slack_notification(BINDING.route, 'alice')
    assert item and item['payload']['text'] == 'private'
    assert store.claim_slack_notification(BINDING.route, 'alice') is None
    store.finish_slack_notification(BINDING.route, 'bob', item['id'], '123')
    assert store.slack_delivery_status('alice')[0]['status'] == 'sending'
    store.finish_slack_notification(BINDING.route, 'alice', item['id'], '123')
    assert store.slack_delivery_status('alice')[0]['status'] == 'sent'
