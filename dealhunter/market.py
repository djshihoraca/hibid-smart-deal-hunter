"""Evidence-based local resale estimates, not invented demand predictions."""
import math
import statistics
import re
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit, parse_qs, unquote

from dealhunter.reefapi import numeric

PLATFORMS = ('Facebook Marketplace', 'Kijiji')


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Evidence timestamps must include a timezone')
    return parsed


def postal_key(value):
    return ''.join(str(value).upper().split())


def platform_url(value, platform):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        root = 'facebook.com' if platform == 'Facebook Marketplace' else 'kijiji.ca'
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and (host == root or host.endswith('.' + root)))
    except (ValueError, TypeError):
        return False


def source_identity(url, platform):
    parsed = urlsplit(url)
    path = unquote(parsed.path).rstrip('/')
    if platform == 'Facebook Marketplace':
        found = re.search(r'/marketplace/item/(\d+)(?:/|$)', path)
        if not found:
            raise ValueError('Use the canonical Facebook Marketplace item URL')
        return platform, str(int(found.group(1)))
    identifier = path.rsplit('/', 1)[-1]
    if not identifier.isdigit():
        identifier = parse_qs(parsed.query).get('adId', [''])[0]
    if not identifier.isdigit():
        raise ValueError('Use a Kijiji listing URL with an identifiable ad ID')
    return platform, str(int(identifier))


def quartiles(values):
    ordered = sorted(values)
    # Interpolated quartiles are intentionally not extrapolated outside the sample.
    def at(fraction):
        index = (len(ordered) - 1) * fraction
        low, high = math.floor(index), math.ceil(index)
        return ordered[low] + (ordered[high] - ordered[low]) * (index - low)
    return {'low': round(at(.25), 2), 'median': round(statistics.median(ordered), 2), 'high': round(at(.75), 2)}


def research_market(records, *, model, conditions, centre='L7A 4X1', radius_km=50, now=None):
    now = now or datetime.now(timezone.utc)
    if not model.strip() or not conditions or not 0 < float(radius_km) <= 50:
        raise ValueError('Exact model, acceptable equivalent conditions and a radius up to 50 km are required')
    if not isinstance(records, list) or len(records) > 2000:
        raise ValueError('Provide at most 2,000 comparable records')
    sold, active, excluded, seen = [], [], [], set()
    for index, record in enumerate(records):
        try:
            if not isinstance(record, dict):
                raise ValueError('Invalid record')
            platform = record.get('platform')
            url = record.get('url', '')
            if platform not in PLATFORMS or not platform_url(url, platform):
                raise ValueError('A matching Facebook Marketplace or Kijiji source URL is required')
            identity = source_identity(url, platform)
            if identity in seen:
                raise ValueError('Duplicate source')
            seen.add(identity)
            if str(record.get('model', '')).strip().casefold() != model.strip().casefold():
                raise ValueError('Model differs')
            if record.get('condition') not in conditions or record.get('currency') != 'CAD':
                raise ValueError('Condition or currency differs')
            distance = numeric(record.get('distance_km'))
            if distance is None or distance > Decimal(str(radius_km)) or postal_key(record.get('centre_postal')) != postal_key(centre):
                raise ValueError('Outside radius or distance measured from another centre')
            observed = timestamp(record.get('observed_at'))
            age_days = (now - observed).total_seconds() / 86400
            if not 0 <= age_days <= 30:
                raise ValueError('Observation is future dated or older than 30 days')
            if record.get('status') == 'sold':
                if record.get('sale_verified') is not True or not str(record.get('sale_evidence', '')).strip():
                    raise ValueError('Disappeared listings and unverified sale claims are not completed-sale evidence')
                sold_at, listed_at = timestamp(record.get('sold_at')), timestamp(record.get('listed_at'))
                if not listed_at <= sold_at <= observed or (now - sold_at).total_seconds() > 30 * 86400:
                    raise ValueError('Invalid sale dates or sale outside the recent window')
                price = numeric(record.get('sold_price'))
                if price is None or not Decimal('.01') <= price <= Decimal('1000000000'):
                    raise ValueError('Verified sold price required; asking price is not sale price')
                sold.append({'url': url, 'platform': platform, 'sold_price': float(price),
                             'days_to_sell': (sold_at - listed_at).total_seconds() / 86400})
            elif record.get('status') == 'active':
                price = numeric(record.get('asking_price'))
                if price is None or not Decimal('.01') <= price <= Decimal('1000000000') or age_days > 7:
                    raise ValueError('Active asking-price evidence must be positive and at most 7 days old')
                active.append({'url': url, 'platform': platform, 'asking_price': float(price)})
            else:
                raise ValueError('Only active listings and verified completed sales are comparable evidence')
        except (ValueError, TypeError, OverflowError) as exc:
            excluded.append({'row': index + 1, 'reason': str(exc)})
    prices = quartiles([r['sold_price'] for r in sold]) if len(sold) >= 3 else None
    days = quartiles([r['days_to_sell'] for r in sold]) if len(sold) >= 3 else None
    return {'agent': 'Market research', 'model': model, 'conditions': conditions, 'centre': centre,
            'radius_km': radius_km, 'window_days': 30, 'evidence_kind': 'User-supplied evidence; not independently live verified',
            'verified_sold_sample': len(sold), 'active_sample': len(active),
            'observed_sold_sample_per_day': round(len(sold) / 30, 3),
            'asking_price_snapshot_cad': quartiles([r['asking_price'] for r in active]) if active else None,
            'resale_price_cad': prices, 'historical_days_to_sell': days,
            'confidence': 'insufficient' if len(sold) < 3 else 'limited' if len(sold) < 10 else 'moderate sample evidence',
            'demand': 'No population-level demand or guaranteed sale date can be inferred from this sample.',
            'limitations': ['Sold-only duration estimates exclude unsold listings and may be optimistic.',
                            'Asking prices and disappeared listings do not establish completed sales.',
                            'Buyer demand, seasonality and future prices can differ from observed sales.'],
            'sold_evidence': sold, 'active_evidence': active, 'excluded': excluded,
            'generated_at': now.isoformat()}
