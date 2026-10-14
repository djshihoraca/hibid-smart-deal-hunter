"""ReefAPI HiBid read-only client, based on the supplied provider documentation."""
import ipaddress
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests

from dealhunter.integrations import https_url

BASE_URL = "https://api.reefapi.com/hibid/v1/"
MAX_RESPONSE = 5 * 1024 * 1024
SORTS = {"lot_number", "newest", "ending_soonest", "sale_order", "bid_high_first",
         "bid_low_first", "most_bids", "fewest_bids", "most_viewed", "most_watched", "last_bid", "hot_rank"}
STATUSES = {"all", "open", "closing", "closed", "hot", "featured", "top"}
LOT_FIELDS = {"lot_id", "url", "title", "description", "lot_number", "quantity", "estimate",
              "currency", "current_bid", "bid_count", "next_bid_amount", "price_realized",
              "price_realized_note", "reserve_met", "state", "is_closed", "is_sold", "closes_at",
              "closes_at_source", "time_left_seconds", "images", "picture_count", "shipping_offered",
              "auction", "auction_house", "pictures", "category_path", "bids", "buy_now", "buy_now_price"}
SECRET_FIELDS = {"apikey", "xapikey", "reefapikey", "authorization", "password", "token", "accesstoken", "cookie"}


class ReefAPIError(ValueError):
    pass


def numeric(value):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def lot_id(value):
    value = str(value)
    if not value.isascii() or not value.isdigit() or not 0 < int(value) or len(value) > 20:
        raise ReefAPIError("Provider returned an invalid lot identifier")
    return value


def radius_miles(km):
    value = numeric(km)
    if value is None or not 1 <= value <= 500:
        raise ReefAPIError("Pickup radius must be between 1 and 500 km")
    return max(1, math.floor(value / Decimal("1.609344")))


def public_image_url(value):
    # These URLs are displayed by the browser, never fetched with the ReefAPI key.
    if not isinstance(value, str) or len(value) > 4096 or not https_url(value):
        return False
    host = urlsplit(value).hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def image_urls(lot):
    result = []
    for field in ("pictures", "images"):
        values = lot.get(field, [])
        if not isinstance(values, list):
            continue
        for value in values:
            url = value if isinstance(value, str) else value.get("url") if isinstance(value, dict) else None
            if public_image_url(url) and url not in result:
                result.append(url)
    return result[:100]


def clean(value, api_key="", depth=0):
    if depth > 32:
        raise ReefAPIError("Provider response is too deeply nested")
    if isinstance(value, dict):
        return {k: clean(v, api_key, depth + 1) for k, v in value.items()
                if "".join(c for c in k.casefold() if c.isalnum()) not in SECRET_FIELDS}
    if isinstance(value, list):
        return [clean(v, api_key, depth + 1) for v in value]
    if isinstance(value, str) and api_key:
        return value.replace(api_key, "[redacted]")
    return value


def normalized_lot(value):
    if not isinstance(value, dict):
        raise ReefAPIError("Provider returned an invalid listing")
    result = {key: item for key, item in value.items() if key in LOT_FIELDS}
    result["lot_id"] = lot_id(result.get("lot_id"))
    for field in ("title", "description"):
        if field in result and not isinstance(result[field], str):
            raise ReefAPIError("Provider returned invalid listing text")
    if not result.get("title"):
        result["title"] = "Lot " + result["lot_id"]
    url = result.get("url", "")
    host = urlsplit(url).hostname if isinstance(url, str) and https_url(url) else ""
    if not host or (host != "hibid.com" and not host.endswith(".hibid.com")):
        result["url"] = "https://hibid.com/lot/" + result["lot_id"]
    for field in ("current_bid", "next_bid_amount", "price_realized", "buy_now_price"):
        if field in result:
            number = numeric(result[field])
            result[field] = str(number) if number is not None else None
    for field in ("auction", "auction_house"):
        if field in result and not isinstance(result[field], dict):
            raise ReefAPIError("Provider returned invalid auction metadata")
    result["currency"] = result.get("currency", "").upper() if isinstance(result.get("currency"), str) else ""
    return result


def review_imported_bid(decision, snapshot, proposed):
    """Never treat a closed, non-CAD or below-minimum imported proposal as eligible."""
    if not snapshot:
        return decision
    lot = snapshot['lot']
    reasons = list(decision['reasons'])
    if lot.get('currency') != 'CAD':
        reasons.append('Imported auction currency is not CAD; no conversion is available.')
    if lot.get('is_closed') is True:
        reasons.append('Imported auction was already closed at fetch.')
    minimum = numeric(lot.get('next_bid_amount'))
    if minimum is not None and numeric(proposed) is not None and numeric(proposed) < minimum:
        reasons.append('Proposed bid is below the provider-reported next bid required; enter a valid bid and refresh auction data.')
    if reasons != decision['reasons']:
        return {'status': 'rejected', 'reasons': reasons, 'approved': False}
    return decision


class ReefAPI:
    def __init__(self, api_key):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ReefAPIError("Configure REEFAPI_KEY in environment variables or Streamlit Secrets")
        self.api_key = api_key

    def _call(self, action, payload):
        if action not in {"search", "detail", "categories"}:
            raise ReefAPIError("Unsupported read-only API action")
        try:
            with requests.post(BASE_URL + action, headers={"x-api-key": self.api_key},
                               json=payload, timeout=(10, 40), allow_redirects=False, stream=True) as response:
                if response.status_code in (401, 403):
                    raise ReefAPIError("ReefAPI rejected access; check your API key and provider account")
                if response.status_code == 429:
                    raise ReefAPIError("ReefAPI rate limit reached; wait before trying again")
                if response.status_code != 200:
                    raise ReefAPIError("ReefAPI did not return a successful response")
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise ReefAPIError("ReefAPI response exceeds the supported size")
                envelope = json.loads(content)
        except requests.RequestException:
            raise ReefAPIError("Could not reach api.reefapi.com; check hosting network access") from None
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise ReefAPIError("ReefAPI returned an invalid JSON response") from None
        if not isinstance(envelope, dict) or envelope.get("ok") is not True:
            raise ReefAPIError("ReefAPI reported an unsuccessful call; check credits, parameters and provider status")
        if not isinstance(envelope.get("data"), dict):
            raise ReefAPIError("ReefAPI returned an invalid data envelope")
        data = clean(envelope["data"], self.api_key)
        meta = envelope.get("meta", {})
        credits = numeric(meta.get("credits")) if isinstance(meta, dict) else None
        return data, str(credits) if credits is not None else None

    def categories(self):
        data, credits = self._call("categories", {})
        roots = data.get("categories")
        if not isinstance(roots, list):
            raise ReefAPIError("ReefAPI returned an invalid category tree")
        found = {}
        def visit(nodes, depth=0):
            if depth > 32 or len(found) > 5000:
                raise ReefAPIError("ReefAPI category tree is too large")
            for node in nodes:
                if not isinstance(node, dict):
                    continue  # Numeric child references are already represented by their nodes.
                identifier = lot_id(node.get("id"))
                name = node.get("full_name") or node.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ReefAPIError("ReefAPI returned an invalid category label")
                found[int(identifier)] = name
                if isinstance(node.get("children"), list):
                    visit(node["children"], depth + 1)
        visit(roots)
        return {"categories": found, "credits": credits}

    def search(self, *, query="", postcode="", km=50, province="", category_ids=(),
               page=1, max_results=10, sort="ending_soonest", status="open"):
        if sort not in SORTS or status not in STATUSES:
            raise ReefAPIError("Unsupported sort or status")
        if type(page) is not int or page < 1 or type(max_results) is not int or not 1 <= max_results <= 100:
            raise ReefAPIError("Invalid search page or page size")
        if page > math.ceil(10000 / max_results):
            raise ReefAPIError("Page exceeds the documented live catalogue limit")
        if province and province not in {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}:
            raise ReefAPIError("Use a two-letter Canadian province or territory code")
        categories = list(dict.fromkeys(category_ids))
        if len(categories) > 5 or any(type(c) is not int or c <= 0 for c in categories):
            raise ReefAPIError("Choose at most five provider category IDs")
        payload = {"country": "Canada", "status": status, "archive": False, "sort": sort,
                   "page": page, "max_results": max_results}
        if query.strip():
            payload["query"] = query.strip()
        if postcode.strip():
            payload.update(zip=postcode.strip(), radius_miles=radius_miles(km))
        if province:
            payload["state"] = province
        lots, seen, reports = [], set(), []
        for category in categories or [None]:
            body = payload | ({"category_id": category} if category is not None else {})
            data, credits = self._call("search", body)
            if not isinstance(data.get("results"), list) or len(data["results"]) > max_results:
                raise ReefAPIError("ReefAPI returned an invalid search result page")
            for raw in data["results"]:
                lot = normalized_lot(raw)
                if lot["lot_id"] not in seen:
                    lots.append(lot)
                    seen.add(lot["lot_id"])
            reports.append({"category_id": category, "total_available": data.get("total_available"),
                            "total_is_capped": data.get("total_is_capped"), "page_count": data.get("page_count"),
                            "credits": credits})
        return {"results": lots, "reports": reports, "page": page, "filters": payload,
                "category_ids": categories, "fetched_at": datetime.now(timezone.utc).isoformat()}

    def detail(self, identifier):
        identifier = lot_id(identifier)
        data, credits = self._call("detail", {"lot": identifier, "include_bids": False})
        lot = normalized_lot(data)
        if lot["lot_id"] != identifier:
            raise ReefAPIError("Provider returned details for a different lot")
        return {"lot": lot, "credits": credits, "fetched_at": datetime.now(timezone.utc).isoformat()}
