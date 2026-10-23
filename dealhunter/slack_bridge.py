"""Private Slack channel transport; exact approvals never execute external actions."""
import json
import re
from dataclasses import dataclass

from dealhunter.agents import scope_hash

HELP = ('Commands: /deals status, jobs, job ID, actions, action ID, watchlists, '
        'scout INDEX CATEGORY_IDS, note YOUR MESSAGE, notes. '
        'Use Review and approve on an action card for exact approval. '
        'A chat message never approves, bids or publishes. No bidding/publishing connector is installed.')


@dataclass(frozen=True)
class Binding:
    owner: str
    team_id: str
    user_id: str
    channel_id: str

    @property
    def route(self):
        return scope_hash(self.__dict__)


def load_bindings(raw, users):
    """Administrator-controlled identity mapping, never supplied by Slack messages."""
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or not values or len(values) > 100:
            raise ValueError
        bindings = [Binding(**v) for v in values]
        identities, destinations, owners = set(), set(), set()
        for b in bindings:
            if b.owner not in users or not users[b.owner]:
                raise ValueError
            if not all(isinstance(v, str) and re.fullmatch(pattern, v) for v, pattern in (
                (b.team_id, r'T[A-Z0-9]+'), (b.user_id, r'[UW][A-Z0-9]+'), (b.channel_id, r'[CG][A-Z0-9]+'))):
                raise ValueError
            identity, destination = (b.team_id, b.user_id), (b.team_id, b.channel_id)
            if identity in identities or destination in destinations or b.owner in owners:
                raise ValueError
            identities.add(identity); destinations.add(destination); owners.add(b.owner)
        return bindings
    except (ValueError, TypeError, KeyError):
        raise ValueError('SLACK_BINDINGS must map unique existing app owners to unique Slack users and private channels') from None


def verify_routes(client, bindings):
    identity = client.auth_test()
    if not identity.get('ok') or not identity.get('bot_id'):
        raise ValueError('A Slack bot token is required')
    for b in bindings:
        if b.team_id != identity.get('team_id'):
            raise ValueError('Slack workspace does not match the configured binding')
        info = client.conversations_info(channel=b.channel_id)
        channel = info.get('channel', {})
        if (not info.get('ok') or channel.get('id') != b.channel_id or channel.get('is_private') is not True
                or channel.get('is_shared') or channel.get('is_ext_shared') or channel.get('is_archived')
                or channel.get('is_member') is not True):
            raise ValueError('Invite the bot into an unshared private channel before enabling its binding')
        # A typo in the approving user must fail before any private notifications leave the app.
        cursor = None
        while True:
            response = client.conversations_members(channel=b.channel_id, limit=200, **({'cursor': cursor} if cursor else {}))
            if not response.get('ok'):
                raise ValueError('Unable to verify the approving channel member')
            if b.user_id in response.get('members', []):
                break
            cursor = response.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                raise ValueError('The configured approver must belong to the private channel')


def plain(text):
    return {'type': 'section', 'text': {'type': 'plain_text', 'text': str(text) or '(empty)', 'emoji': False}}


def document_blocks(value):
    """Full untruncated scope; refuse oversized reviews rather than hide approved fields."""
    text = json.dumps(value, indent=2, ensure_ascii=False)
    if len(text) > 36000:
        raise ValueError('This record is too large for full Slack review; use the authenticated dashboard')
    return [plain(text[i:i+2500]) for i in range(0, len(text), 2500)]


def action_card(action):
    payload = action['payload']
    if payload['kind'] == 'bid':
        summary = (f"HiBid bid: lot {payload.get('lot_id')}\nExact hammer: CAD {payload.get('hammer_cad')}\n"
                   f"Maximum all-in: CAD {payload.get('maximum_all_in_cad')}\n"
                   f"Estimated profit range: {payload.get('estimated_profit_range_cad')}\n"
                   f"Warnings: {str(payload.get('warnings', []))[:700]}")
    else:
        summary = (f"{payload.get('platform')} ad: {str(payload.get('title', ''))[:300]}\n"
                   f"Asking price: CAD {payload.get('asking_price_cad')}\n"
                   f"Ownership confirmed: {payload.get('owned') is True}; authorized photos: {payload.get('photos_authorized') is True}")
    blocks = [plain(f"Action #{action['id']} · {action['status']}"), plain(summary),
              plain('Proposal only. No external bidding or publishing connector is installed. Review the entire exact scope before approval; photos never prove functionality.')]
    if action['status'] in ('proposed', 'approved'):
        value = json.dumps({'id': action['id'], 'fingerprint': action['fingerprint']})
        buttons = []
        if action['status'] == 'proposed':
            buttons.append({'type': 'button', 'text': {'type': 'plain_text', 'text': 'Review and approve'},
                            'action_id': 'deals_review', 'value': value})
        buttons.append({'type': 'button', 'text': {'type': 'plain_text', 'text': 'Revoke action'},
                        'action_id': 'deals_revoke', 'value': value})
        blocks.append({'type': 'actions', 'elements': buttons})
    return {'text': f"HiBid Smart Deal Hunter action #{action['id']}: {action['status']}", 'blocks': blocks}


class SlackBridge:
    def __init__(self, store, client, bindings):
        self.store, self.client, self.bindings = store, client, bindings
        self.since = {b.route: store.activate_slack_route(b.route) for b in bindings}

    def binding(self, team, user, channel):
        return next((b for b in self.bindings if (b.team_id, b.user_id, b.channel_id) == (team, user, channel)), None)

    def send(self, binding, text=None, blocks=None, **unused):
        # No response_url, arbitrary webhook, user-provided URL or public destination.
        verify_routes(self.client, [binding])
        response = self.client.chat_postMessage(channel=binding.channel_id, text=text or 'HiBid Smart Deal Hunter',
                                                blocks=blocks, unfurl_links=False, unfurl_media=False,
                                                parse='none', link_names=False)
        if not response.get('ok') or not response.get('ts'):
            raise ValueError('Slack did not confirm delivery')
        return response['ts']

    def action(self, binding, identifier, fingerprint=None):
        action = next((a for a in self.store.agent_actions(binding.owner) if a['id'] == identifier), None)
        if not action or scope_hash(action['payload']) != action['fingerprint'] or (fingerprint and action['fingerprint'] != fingerprint):
            raise ValueError('Action unavailable or changed. Refresh the private dashboard.')
        return action

    def poll(self):
        for b in self.bindings:
            for action in self.store.agent_actions(b.owner):
                if action['created_at'] >= self.since[b.route]:
                    key = f"action:{action['id']}:{action['fingerprint']}:{action['status']}"
                    self.store.enqueue_slack_notification(b.route, b.owner, key, action_card(action))
            for job in self.store.agent_jobs(b.owner):
                if job['created_at'] >= self.since[b.route] and job['status'] in ('succeeded', 'failed', 'blocked'):
                    result = job.get('result') or {}
                    summary = f"Job #{job['id']} · {job['agent']} · {job['status']}\n{str(result.get('status', ''))[:300]}"
                    if job['agent'] == 'Market research' and result:
                        summary += (f"\nSold sample: {result.get('verified_sold_sample')}; confidence: {result.get('confidence')}"
                                    f"\nResale price CAD: {result.get('resale_price_cad')}"
                                    f"\nHistorical days to sell: {result.get('historical_days_to_sell')}"
                                    '\nUser-supplied evidence; no live market feed. Historical durations are not a forecast.')
                    if job.get('error'):
                        summary += '\n' + job['error']
                    summary += f"\nUse /deals job {job['id']} for findings. No external bid or ad submission."
                    self.store.enqueue_slack_notification(b.route, b.owner, f"job:{job['id']}:{job['status']}",
                                                          {'text': f"HiBid agent job #{job['id']}: {job['status']}", 'blocks': [plain(summary)]})
            # At most five deliveries per route per poll. Claimed/uncertain sends are not retried automatically.
            for _ in range(5):
                item = self.store.claim_slack_notification(b.route, b.owner)
                if not item:
                    break
                ts = None
                try:
                    ts = self.send(b, **item['payload'])
                except Exception:
                    pass  # Never log provider bodies/tokens or falsely mark delivery as successful.
                self.store.finish_slack_notification(b.route, b.owner, item['id'], ts)

    def command(self, payload, request_key):
        b = self.binding(payload.get('team_id'), payload.get('user_id'), payload.get('channel_id'))
        if not b or payload.get('command') != '/deals':
            return
        if not self.store.claim_slack_request(scope_hash([b.route, payload.get('trigger_id') or request_key])):
            return
        text = str(payload.get('text', '')).strip()
        parts = text.split()
        verb = parts[0].lower() if parts else 'help'
        try:
            if verb == 'status':
                jobs = self.store.agent_jobs(b.owner)
                actions = self.store.agent_actions(b.owner)
                self.send(b, blocks=[plain(f"Latest inbox: {len(jobs)} jobs; {sum(a['status']=='proposed' for a in actions)} pending actions.\n"
                                          'Approval records intent only. Bidding/publishing remain disconnected.\n' + HELP)])
            elif verb in ('jobs', 'actions', 'notes', 'watchlists'):
                if verb == 'jobs':
                    rows = [f"#{j['id']} · {j['agent']} · {j['status']}" for j in self.store.agent_jobs(b.owner)[:10]]
                elif verb == 'actions':
                    rows = [f"#{a['id']} · {a['payload'].get('platform', 'HiBid bid')} · {a['status']}" for a in self.store.agent_actions(b.owner)[:10]]
                elif verb == 'notes':
                    rows = [f"#{n['id']}: {n['content'][:200]}" for n in self.store.agent_notes(b.owner)[:10]]
                else:
                    rows = [f"{i+1}: {w['name'][:150]}" for i, w in enumerate(self.store.watchlists(b.owner))]
                self.send(b, blocks=[plain('\n'.join(rows)[:2800] or 'No saved records.')])
            elif verb == 'job' and len(parts) == 2:
                job = self.store.agent_job(b.owner, int(parts[1]))
                if not job:
                    raise ValueError('Job not found for your account')
                self.send(b, blocks=document_blocks(job))
            elif verb == 'action' and len(parts) == 2:
                self.send(b, **action_card(self.action(b, int(parts[1]))))
            elif verb == 'scout' and len(parts) in (2, 3):
                watches = self.store.watchlists(b.owner)
                index = int(parts[1]) - 1
                if not 0 <= index < len(watches):
                    raise ValueError('Choose an index from /deals watchlists')
                categories = [int(c) for c in parts[2].split(',')] if len(parts) == 3 else []
                if len(categories) > 5 or any(c <= 0 for c in categories) or (watches[index].get('categories') and not categories):
                    raise ValueError('Supply up to five positive provider category IDs for categorized watchlists')
                identifier = self.store.enqueue_agent_job(b.owner, 'Scout', {'watchlist': watches[index], 'category_ids': list(dict.fromkeys(categories))})
                self.send(b, blocks=[plain(f'Scout job #{identifier} queued. A worker must run it; no bid is authorized.')])
            elif verb == 'note':
                content = text[len(parts[0]):].strip()
                identifier = self.store.add_agent_note(b.owner, f'Slack {b.team_id}/{b.user_id}', content)
                self.send(b, blocks=[plain(f'Note #{identifier} saved for dashboard review. Notes do not alter jobs, approve actions or execute trades.')])
            else:
                self.send(b, blocks=[plain(HELP)])
        except (ValueError, TypeError):
            self.send(b, blocks=[plain('Request unavailable, invalid, or too large. Check its ID/arguments and use the private dashboard for full records.\n' + HELP)])

    def interaction(self, payload):
        team, user = payload.get('team', {}).get('id'), payload.get('user', {}).get('id')
        if payload.get('type') == 'view_submission':
            return self.submit_view(payload, team, user)
        b = self.binding(team, user, payload.get('channel', {}).get('id'))
        if not b or payload.get('type') != 'block_actions':
            return
        values = payload.get('actions', [])
        if len(values) != 1 or values[0].get('action_id') not in ('deals_review', 'deals_revoke'):
            return
        try:
            selected = json.loads(values[0]['value'])
            action = self.action(b, int(selected['id']), selected['fingerprint'])
            if values[0]['action_id'] == 'deals_revoke':
                self.store.revoke_agent_action(b.owner, action['id'])
                self.send(b, blocks=[plain(f"Action #{action['id']} revoked. Nothing was submitted.")])
                return
            if action['status'] != 'proposed':
                raise ValueError('This action is no longer pending')
            blocks = [plain('Review every field below. Approval records intent only; it does not submit a bid or publish an ad. Photos are not proof of functionality. Review the saved photo assets in the dashboard before confirming them.')]
            blocks += document_blocks(action['payload'])
            blocks += [
                {'type': 'input', 'block_id': 'consent', 'label': {'type': 'plain_text', 'text': 'Exact-action confirmation'},
                 'element': {'type': 'checkboxes', 'action_id': 'consent', 'options': [
                     {'text': {'type': 'plain_text', 'text': 'I approve this exact lot, costs, platform, text and photo references.'}, 'value': 'yes'}]}},
                {'type': 'input', 'block_id': 'reason', 'label': {'type': 'plain_text', 'text': 'Approval reason'},
                 'element': {'type': 'plain_text_input', 'action_id': 'reason', 'min_length': 3, 'max_length': 500}},
            ]
            metadata = {'channel': b.channel_id, 'id': action['id'], 'fingerprint': action['fingerprint']}
            self.client.views_open(trigger_id=payload['trigger_id'], view={
                'type': 'modal', 'callback_id': 'deals_approval', 'private_metadata': json.dumps(metadata),
                'title': {'type': 'plain_text', 'text': f"Approve action #{action['id']}"[:24]},
                'submit': {'type': 'plain_text', 'text': 'Record approval'},
                'close': {'type': 'plain_text', 'text': 'Cancel'}, 'blocks': blocks})
        except (ValueError, KeyError, TypeError):
            self.send(b, blocks=[plain('This action is unavailable for Slack review. Refresh the dashboard; no approval was recorded.')])

    def submit_view(self, payload, team, user):
        # No network call before acknowledgement: only fast, local validation and atomic approval.
        try:
            view = payload['view']
            if view.get('callback_id') != 'deals_approval':
                raise ValueError
            metadata = json.loads(view['private_metadata'])
            b = self.binding(team, user, metadata['channel'])
            if not b:
                raise ValueError
            state = view['state']['values']
            if [o.get('value') for o in state['consent']['consent'].get('selected_options', [])] != ['yes']:
                return {'response_action': 'errors', 'errors': {'consent': 'Explicit confirmation is required.'}}
            reason = state['reason']['reason'].get('value', '')
            if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500:
                return {'response_action': 'errors', 'errors': {'reason': 'Enter a reason of 3–500 characters.'}}
            action = self.action(b, int(metadata['id']), metadata['fingerprint'])
            self.store.approve_agent_action(b.owner, action['id'], action['fingerprint'], f'Slack {team}/{user}: {reason.strip()}')
            updated = self.action(b, action['id'], action['fingerprint'])
            key = f"action:{updated['id']}:{updated['fingerprint']}:{updated['status']}"
            self.store.enqueue_slack_notification(b.route, b.owner, key, action_card(updated))
            return {}
        except (ValueError, KeyError, TypeError):
            return {'response_action': 'errors', 'errors': {'reason': 'Approval unavailable: account, action or state changed. Refresh the dashboard.'}}
