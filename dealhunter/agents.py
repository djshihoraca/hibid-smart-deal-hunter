"""Specialist tool agents orchestrated through durable jobs and explicit approvals."""
import hashlib
import json
import re
from decimal import Decimal
from urllib.parse import urlsplit

from dealhunter.calculator import calculate
from dealhunter.market import PLATFORMS, research_market
from dealhunter.reefapi import ReefAPI, numeric

AGENTS = ('Scout', 'Inspection', 'Market research', 'Bid assistant', 'Ad writer')


def scope_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def hibid_target(record):
    parsed = urlsplit(record.get('listing_url', ''))
    host = parsed.hostname or ''
    match = re.search(r'/lot/(\d+)(?:/|$)', parsed.path)
    if parsed.scheme != 'https' or parsed.username or parsed.password or not (host == 'hibid.com' or host.endswith('.hibid.com')) or not match:
        raise ValueError('A valid HiBid lot URL is required for a bid handoff')
    return {'lot_id': match.group(1), 'url': record['listing_url']}


class ScoutAgent:
    def run(self, payload, context):
        watch = payload['watchlist']
        queries = watch.get('models') or watch.get('brands') or watch.get('keywords') or ['']
        if len(queries) > 3:
            raise ValueError('Choose up to three model/brand/keyword queries per run to bound credit usage')
        radius = min(50, int(watch.get('radius_km', 50)))
        categories = payload.get('category_ids', [])
        if len(categories) > 5 or any(type(c) is not int or c <= 0 for c in categories):
            raise ValueError('Use at most five positive integer provider category IDs')
        client = ReefAPI(context.get('reef_key', ''))
        if watch.get('categories') and not categories:
            raise ValueError('Explicit provider category IDs are required for categorized watchlists')
        context['store'].reserve_scout_calls(context['owner'], len(queries) * max(1, len(categories)))
        candidates, seen = [], set()
        for query in queries:
            result = client.search(query=query, postcode=watch.get('postal', 'L7A 4X1'), km=radius,
                                   category_ids=categories, max_results=10)
            for lot in result['results']:
                if lot['lot_id'] in seen:
                    continue
                seen.add(lot['lot_id'])
                text = (lot.get('title', '') + ' ' + lot.get('description', '')).casefold()
                if any(watch.get(field) and not any(str(word).casefold() in text for word in watch[field])
                       for field in ('brands', 'models', 'keywords')):
                    continue
                if lot.get('is_closed') is True or lot.get('currency') != 'CAD':
                    continue
                snapshot = context['store'].save_auction_snapshot(context['owner'], lot, result['fetched_at'])
                inspection_job = context['store'].enqueue_agent_job(context['owner'], 'Inspection', {'auction_lot': lot})
                candidates.append({'snapshot_id': snapshot, 'lot_id': lot['lot_id'], 'title': lot['title'],
                                   'url': lot['url'], 'current_bid': lot.get('current_bid'),
                                   'inspection_job_id': inspection_job,
                                   'status': 'requires condition, pickup, retail and fee verification'})
        return {'agent': 'Scout', 'candidates': candidates, 'radius_km': radius,
                'limitations': 'Category IDs must be chosen explicitly; seller condition and actual pickup distance are not independently verified.'}


class InspectionAgent:
    def run(self, payload, context):
        if payload.get('auction_lot'):
            from dealhunter.inspection import assess
            from dealhunter.reefapi import image_urls
            lot = payload['auction_lot']
            return {'agent': 'Inspection', 'lot_id': lot['lot_id'], 'status': 'requires manual verification',
                    'assessment': assess(lot.get('description', ''), [], len(image_urls(lot))),
                    'limitations': 'Description signals only. Photo contents, exact model, retail price and condition remain unverified.'}
        record = payload['inspection']
        return {'agent': 'Inspection', 'decision': record['decision'], 'assessment': record['assessment'],
                'all_in_cad': record['calculation']['current_total'],
                'hammer_ceiling_cad': record['calculation']['max_hammer'],
                'limitations': 'This reviews the saved inspection; it does not refresh auction or retail prices.'}


class MarketResearchAgent:
    def run(self, payload, context):
        record = payload['inspection']
        watch = record.get('watchlist', {})
        source = record.get('selected_price_source') or next(iter(record.get('price_sources', [])), {})
        equivalent = [source['condition']] if source.get('condition') in record['conditions'] else record['conditions']
        if len(equivalent) != 1:
            raise ValueError('Choose one verified equivalent product condition before market comparison')
        return research_market(payload.get('comparables', []), model=record['name'], conditions=equivalent,
                               centre=watch.get('postal', 'L7A 4X1'), radius_km=min(50, watch.get('radius_km', 50)))


class BidAssistantAgent:
    def run(self, payload, context):
        record = payload['inspection']
        if record['decision']['status'] != 'eligible':
            return {'agent': 'Bid assistant', 'status': 'blocked', 'reasons': record['decision']['reasons']}
        calculation = calculate(**record['bid_inputs'])
        if not calculation['meets_discount'] and not record['decision'].get('approved'):
            return {'agent': 'Bid assistant', 'status': 'blocked', 'reasons': ['An explicit discount-ceiling exception is missing.']}
        market = payload.get('market_report')
        if payload.get('market_job_id'):
            job = context['store'].agent_job(context['owner'], payload['market_job_id'])
            market = job.get('result') if job and job['status'] == 'succeeded' else None
        if not market or not market.get('resale_price_cad'):
            return {'agent': 'Bid assistant', 'status': 'blocked: insufficient local completed-sale evidence',
                    'reasons': ['At least three recent, exact-model/equivalent-condition verified sold comparables are needed for a resale recommendation.']}
        if payload.get('resale_costs_confirmed') is not True:
            return {'agent': 'Bid assistant', 'status': 'blocked: resale costs need confirmation'}
        resale_costs = numeric(payload.get('resale_costs', 0))
        target_profit = numeric(payload.get('minimum_profit', 20))
        if resale_costs is None or target_profit is None:
            raise ValueError('Resale costs and target profit must be nonnegative')
        profit_low = Decimal(str(market['resale_price_cad']['low'])) - resale_costs - calculation['current_total']
        profit_high = Decimal(str(market['resale_price_cad']['high'])) - resale_costs - calculation['current_total']
        if profit_low < target_profit:
            return {'agent': 'Bid assistant', 'status': 'blocked: conservative resale margin is below target',
                    'estimated_profit_range_cad': [str(profit_low), str(profit_high)],
                    'minimum_profit_cad': str(target_profit), 'limitations': market['limitations']}
        target = hibid_target(record)
        action = {'kind': 'bid', **target, 'hammer_cad': str(numeric(record['bid_inputs']['current'])),
                  'maximum_all_in_cad': str(calculation['current_total']), 'assumptions': record['bid_inputs'],
                  'inspection_sha256': scope_hash(record), 'inspection_status': record['decision']['status'],
                  'transport': 'verified_integration_required', 'resource': 'bid:' + target['lot_id'],
                  'requires_live_revalidation': True, 'warnings': record['decision']['reasons']}
        action.update(market_evidence_sha256=scope_hash(market),
                      historical_days_to_sell=market['historical_days_to_sell'],
                      estimated_profit_range_cad=[str(profit_low), str(profit_high)],
                      resale_costs_cad=str(resale_costs), minimum_profit_cad=str(target_profit))
        action_id = context['store'].propose_agent_action(context['owner'], action)
        return {'agent': 'Bid assistant', 'status': 'awaiting exact-bid approval', 'action_id': action_id,
                'details': action, 'limitations': 'ReefAPI has no bidding endpoint. No bid is submitted by this agent.'}


class AdWriterAgent:
    def run(self, payload, context):
        record = payload['inspection']
        platform = payload.get('platform')
        if platform not in PLATFORMS:
            raise ValueError('Choose Facebook Marketplace or Kijiji')
        asking = numeric(payload.get('asking_price'))
        if asking is None or asking <= 0:
            raise ValueError('A positive asking price is required')
        # Drafts are templates grounded in user-reviewed facts, not invented features.
        condition = ', '.join(record.get('conditions', [])) or 'Condition needs confirmation'
        owned = payload.get('owned') is True
        body = (f"{record['name']}\n\nAsking CAD ${asking:.2f}.\n"
                f"Condition reported in the inspection: {condition}. Seller must confirm this before publishing.\n"
                f"Pickup area: {record.get('watchlist', {}).get('postal', 'To be arranged')}.\n"
                "Functionality, accessories and product details must be confirmed by the seller.\n")
        if payload.get('confirmed_details'):
            body += '\nSeller-confirmed details:\n' + str(payload['confirmed_details'])[:6000]
        draft = {'kind': 'publish_ad', 'platform': platform, 'title': record['name'], 'body': body,
                 'account_kind': 'personal',
                 'asking_price_cad': str(asking), 'owned': owned,
                 'inspection_sha256': scope_hash(record), 'transport': 'verified_integration_required',
                 'resource': 'ad:' + platform + ':' + record.get('listing_url', record['name']),
                 'photos_authorized': payload.get('photos_authorized') is True,
                 'photo_refs': [{'inspection_id': record['id'], 'filename': name, 'sha256': digest}
                                for name, digest in zip(record.get('photo_filenames', []), record.get('photo_sha256', []))],
                 'photo_policy': 'Use your own authorized photos; auction photos are not automatically licensed.'}
        action_id = context['store'].propose_agent_action(context['owner'], draft)
        return {'agent': 'Ad writer', 'status': 'draft requires ownership confirmation and approval',
                'action_id': action_id, 'draft': draft,
                'limitations': 'No marketplace publishing API is connected. Drafting is not publication.'}


REGISTRY = {name: worker for name, worker in zip(AGENTS, (ScoutAgent, InspectionAgent, MarketResearchAgent, BidAssistantAgent, AdWriterAgent))}


def enqueue_team(store, owner, inspection, comparables, *, asking_price, owned=False, photos_authorized=False,
                 confirmed_details='', resale_costs=0, minimum_profit=20, resale_costs_confirmed=False):
    common = {'inspection': inspection}
    first = store.enqueue_agent_job(owner, 'Inspection', common)
    market = store.enqueue_agent_job(owner, 'Market research', common | {'comparables': comparables, 'depends_on': [first]})
    bid = store.enqueue_agent_job(owner, 'Bid assistant', common | {
        'market_job_id': market, 'depends_on': [market], 'resale_costs': resale_costs,
        'minimum_profit': minimum_profit, 'resale_costs_confirmed': resale_costs_confirmed})
    ads = [store.enqueue_agent_job(owner, 'Ad writer', common | {
        'platform': platform, 'asking_price': asking_price, 'owned': owned,
        'photos_authorized': photos_authorized, 'confirmed_details': confirmed_details,
        'depends_on': [first]}) for platform in PLATFORMS]
    return [first, market, bid, *ads]


def run_next_job(store, owner, reef_key=''):
    job = store.claim_agent_job(owner)
    if job is None:
        return None
    try:
        result = REGISTRY[job['agent']]().run(job['payload'], {'store': store, 'owner': owner, 'reef_key': reef_key})
        store.finish_agent_job(owner, job['id'], result=result)
    except Exception:
        # Do not leak keys, arbitrary provider content or private paths into the phone inbox.
        store.finish_agent_job(owner, job['id'], error='Agent could not complete this task. Check required evidence, configuration and provider access; no external bid or publication was performed.')
    return job['id']
