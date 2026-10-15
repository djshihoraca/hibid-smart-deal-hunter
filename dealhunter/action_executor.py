"""Connector interface for future reviewed integrations; no live adapters are installed."""
from decimal import Decimal

from dealhunter.agents import scope_hash
from dealhunter.reefapi import numeric

# A real adapter must implement prepare(payload) and submit(payload, idempotency_key),
# own secure provider authentication, and have a tested permitted write capability.
# This registry intentionally has no fictional Facebook/Kijiji/HiBid endpoint.
ADAPTERS = {}


def execute_approved(store, owner, identifier, fingerprint, adapter=None):
    if adapter is None:
        raise ValueError('No permitted, tested bidding or publishing integration is connected')
    candidates = [a for a in store.agent_actions(owner) if a['id'] == identifier]
    if not candidates or candidates[0]['status'] != 'approved' or candidates[0]['fingerprint'] != fingerprint:
        raise ValueError('Approve the exact action before requesting execution')
    payload = candidates[0]['payload']
    if getattr(adapter, 'kind', None) != payload['kind'] or getattr(adapter, 'permitted', False) is not True:
        raise ValueError('Connector does not support this permitted action')
    if payload['kind'] == 'publish_ad' and (getattr(adapter, 'platform', None) != payload['platform'] or payload.get('owned') is not True):
        raise ValueError('Publishing requires the exact platform and confirmed ownership')
    if payload['kind'] == 'publish_ad':
        if getattr(adapter, 'account_kind', None) != payload.get('account_kind'):
            raise ValueError('The connector must support the approved personal-account type')
        if payload.get('photos_authorized') is not True or not payload.get('photo_refs'):
            raise ValueError('Authorized, approved photo assets are required')
        import hashlib
        for photo in payload['photo_refs']:
            originals = store.photos(owner, photo['inspection_id'])
            if not any(name == photo['filename'] and hashlib.sha256(raw).hexdigest() == photo['sha256'] for name, raw in originals):
                raise ValueError('An approved photo asset is unavailable or changed')
    prepared = adapter.prepare(payload)
    if not isinstance(prepared, dict) or prepared.get('scope_sha256') != fingerprint or prepared.get('idempotency_supported') is not True:
        raise ValueError('Connector must preserve the approved scope and support idempotent submission')
    if payload['kind'] == 'bid':
        hammer, total = numeric(prepared.get('hammer_cad')), numeric(prepared.get('all_in_cad'))
        if prepared.get('currency') != 'CAD' or prepared.get('is_open') is not True or hammer is None or total is None:
            raise ValueError('Fresh CAD auction state and all-in cost are required')
        if hammer != numeric(payload['hammer_cad']) or total > numeric(payload['maximum_all_in_cad']):
            raise ValueError('Current bid or fees exceed or differ from the exact approved instructions')
    current = store.start_agent_execution(owner, identifier, fingerprint)
    try:
        response = adapter.submit(current, idempotency_key=f'agent-action:{owner}:{identifier}:{fingerprint}')
        if not isinstance(response, dict) or response.get('confirmed') is not True or not isinstance(response.get('external_id'), str) or not response['external_id'].strip():
            raise ValueError('Provider did not return a confirmed external receipt')
        receipt = {'external_id': response['external_id'][:200], 'provider': str(getattr(adapter, 'name', 'connected provider'))[:100]}
        store.finish_agent_execution(owner, identifier, receipt)
        return receipt
    except Exception:
        store.finish_agent_execution(owner, identifier)
        raise ValueError('External outcome is unknown. Do not retry automatically; reconcile with the provider first.') from None
