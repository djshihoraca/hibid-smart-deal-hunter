"""Authenticated, mobile-friendly, manually operated auction dashboard."""
import hashlib
import base64
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from dealhunter.calculator import calculate
from dealhunter.catalogue import amount, catalogue_entries, percentage
from dealhunter.live_catalogue import render_live_catalogue, render_saved_auction_snapshots, show_public_photos
from dealhunter.reefapi import image_urls, numeric, review_imported_bid
from dealhunter.inspection import CATEGORIES, CONDITIONS, assess, decide
from dealhunter.integrations import https_url, live_retail, source_fresh, validate_photo, vision_photos
from dealhunter.security import LoginLimiter, verify_password
from dealhunter.storage import Store, database_path, storage_reason, temporary_database_path, utcnow

st.set_page_config(page_title='HiBid Smart Deal Hunter', page_icon='🔎', layout='wide')
st.markdown('''<style>
@media (max-width: 640px) {
 .block-container {padding: 1rem .8rem 3rem;}
 div[data-testid="stHorizontalBlock"] {flex-direction: column;}
 div[data-testid="stHorizontalBlock"] > div {width: 100% !important; flex: auto;}
 button {min-height: 44px;}
}
</style>''', unsafe_allow_html=True)
st.title('HiBid Smart Deal Hunter')
st.caption('Private decision assistant • Manual listing entry • No automated bidding')


def setting(name, default=''):
    value = os.environ.get(name)
    if value is not None:
        return value
    try:
        return st.secrets.get(name, default)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return default


@st.cache_resource
def login_limiter():
    return LoginLimiter()


def authenticate():
    try:
        config = setting('DEAL_HUNTER_USERS', '{}')
        users = json.loads(config) if isinstance(config, str) else dict(config)
        if not isinstance(users, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in users.items()):
            raise ValueError
    except (ValueError, TypeError):
        st.error('Invalid authentication configuration. Ask the application administrator to fix it.')
        st.stop()
    if not users:
        st.info('Private data is locked. Configure DEAL_HUNTER_USERS with app usernames and password hashes. See README.')
        st.stop()
    auth = st.session_state.get('auth')
    if auth and (time.time() - auth['last_seen'] > 1800 or users.get(auth['owner']) != auth['binding']):
        st.session_state.clear()
        auth = None
    if not auth:
        with st.form('login', clear_on_submit=True):
            username = st.text_input('App username')
            password = st.text_input('App password', type='password', help='Never use your HiBid password.')
            submitted = st.form_submit_button('Sign in', use_container_width=True)
        if submitted:
            if time.time() < st.session_state.get('login_after', 0) or not login_limiter().allow(username):
                st.error('Please wait before trying again.')
            else:
                binding = users.get(username, 'pbkdf2_sha256$600000$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
                valid = verify_password(password, binding)
                if valid and username in users:
                    st.session_state.clear()
                    st.session_state.auth = {'owner': username, 'binding': binding, 'last_seen': time.time()}
                    st.rerun()
                else:
                    st.session_state.login_after = time.time() + 3
                    st.error('Invalid username or password.')
        st.stop()
    auth['last_seen'] = time.time()
    with st.sidebar:
        st.caption(f"Signed in as {auth['owner']}")
        if st.button('Sign out', use_container_width=True):
            st.session_state.clear()
            st.rerun()
    return auth['owner']


owner = authenticate()  # No database or private dashboard before authentication.
storage_source = 'automatic storage discovery'
storage_stage = 'resolve-path'
try:
    if st.session_state.get('temporary_storage'):
        selected_database = Path(st.session_state.temporary_storage)
        storage_source = 'explicit temporary session storage'
    else:
        configured_database = setting('DEAL_HUNTER_DB', None)
        if os.environ.get('DEAL_HUNTER_DB') is not None:
            storage_source = 'environment variable DEAL_HUNTER_DB (takes priority over Secrets)'
        elif configured_database is not None:
            storage_source = 'Streamlit Secrets DEAL_HUNTER_DB'
        selected_database = database_path(configured_database, Path(__file__).parent)
        if configured_database is None:
            storage_source = ('existing checkout history' if selected_database == Path(__file__).parent / 'data' / 'dealhunter.sqlite3'
                              else 'default user storage directory')
    storage_stage = 'open-storage'
    store = Store(selected_database)
except (OSError, sqlite3.Error, ValueError) as exc:
    st.error('Private storage could not be opened. No alternate database has been opened.')
    st.code(f"Storage source: {storage_source}\nDiagnostic: {getattr(exc, 'stage', storage_stage)} / {storage_reason(exc)}", language=None)
    st.write('In Streamlit Cloud, open Manage app → Settings → Secrets. Remove DEAL_HUNTER_DB entirely '
             '(do not leave an empty string), or set it to an actual writable database file path. '
             'An environment variable overrides Secrets. For existing history, check directory ownership '
             'and permissions before changing paths. The old /private/persistent example is not a provisioned volume.')
    st.warning('Optional recovery: a separate temporary database can let you inspect listings now. '
               'It will not contain existing history and is not durable. Export records before signing out; '
               'they may be lost after logout, reboot or redeployment. The original database will not be modified.')
    temporary_ack = st.checkbox('I understand temporary storage is separate and may lose records')
    if st.button('Use temporary storage for this session', disabled=not temporary_ack):
        try:
            st.session_state.temporary_storage = str(temporary_database_path())
        except OSError:
            st.error('Temporary storage is also unavailable. Ask the hosting administrator for a writable private directory.')
        else:
            st.rerun()
    st.stop()
if st.session_state.get('temporary_storage'):
    st.warning('Temporary session storage is active. Existing history is not shown. Export records before signing out; '
               'this storage is not durable across logout, reboot or redeployment.')


def lines(value):
    return list(dict.fromkeys(x.strip() for x in value.splitlines() if x.strip()))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def money(value):
    return f'${value:.2f}'


def display_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(ZoneInfo('America/Toronto')).strftime('%Y-%m-%d %H:%M %Z')


def show_photos(photos):
    # Inline bytes avoid Streamlit's unauthenticated /media resource endpoints.
    from PIL import Image, ImageOps
    import io
    for _, raw in photos:
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((1200, 1200))
            buffer = io.BytesIO()
            image.save(buffer, format='JPEG', quality=85)
        encoded = base64.b64encode(buffer.getvalue()).decode()
        st.markdown(f'<img alt="Private listing photo" style="max-width:100%;width:300px" src="data:image/jpeg;base64,{encoded}">', unsafe_allow_html=True)


def download_record(record, filename):
    # Only generated base64 and controlled filenames enter this HTML.
    payload = base64.b64encode(json.dumps(record, indent=2).encode()).decode()
    st.markdown(f'<a download="{filename}" href="data:application/json;base64,{payload}">Download inspection report (JSON)</a>', unsafe_allow_html=True)


pending = st.session_state.pop('reef_pending_inspection', None)
if pending:
    imported = pending['lot']
    st.session_state.reef_import = pending
    st.session_state.listing_url = imported['url']
    st.session_state.listing_name = imported['title']
    st.session_state.listing_description = imported.get('description', '')
    st.session_state.listing_conditions = []
    next_bid = numeric(imported.get('next_bid_amount'))
    if next_bid is None:
        next_bid = numeric(imported.get('current_bid'))
    st.session_state.proposed_hammer = float(next_bid) if next_bid is not None and next_bid <= 1_000_000_000 else 0.0
    st.session_state.terms_confirmed = False
    st.session_state.uncertainty_confirmed = False
    for stale in ('vision', 'retail', 'manual_check', 'comparison_scope', 'price_sources'):
        st.session_state.pop(stale, None)


watchlists = store.watchlists(owner)
with st.sidebar:
    st.header('Watchlists')
    selected = st.selectbox('Saved watchlist', ['New watchlist'] + [w['name'] for w in watchlists])
    watch = next((w for w in watchlists if w['name'] == selected), {})
    ns = 'watch:' + selected
    watch_name = st.text_input('Watchlist name', value=watch.get('name', ''), key=ns + ':name')
    categories = st.multiselect('Categories', CATEGORIES, default=watch.get('categories', CATEGORIES[:3]), key=ns + ':categories')
    brands = st.text_area('Brands (one per line)', value='\n'.join(watch.get('brands', [])), key=ns + ':brands')
    models = st.text_area('Models (one per line)', value='\n'.join(watch.get('models', [])), key=ns + ':models')
    keywords = st.text_area('Keywords (one per line)', value='\n'.join(watch.get('keywords', [])), key=ns + ':keywords')
    accepted = st.multiselect('Acceptable conditions', CONDITIONS, default=watch.get('conditions', CONDITIONS[:3]), key=ns + ':conditions')
    postal = st.text_input('Search centre', value=watch.get('postal', 'L7A 4X1'), key=ns + ':postal')
    radius = st.number_input('Pickup radius (km)', 1, 500, value=watch.get('radius_km', 50), key=ns + ':radius')
    watch_payload = {'categories': categories, 'brands': lines(brands), 'models': lines(models),
                     'keywords': lines(keywords), 'conditions': accepted, 'postal': postal, 'radius_km': radius}
    if st.button('Save watchlist', use_container_width=True):
        if watch_name.strip() and accepted:
            store.save_watchlist(owner, watch_name, watch_payload)
            st.success('Watchlist saved. It will appear in the saved list on your next interaction.')
        else:
            st.error('Enter a name and at least one acceptable condition.')
    discount = st.slider('Required savings (%)', 70, 95, 70)
    st.info('Browse live lots through the ReefAPI catalogue, or add listings manually. Watchlists do not submit bids or monitor auctions in the background.')

catalogue_tab, inspect_tab, history_tab, agents_tab = st.tabs(['Catalogue', 'Inspect listing', 'Inspection history', 'Agent team'])
with inspect_tab:
    st.header('Inspect an auction listing')
    url = st.text_input('HiBid listing URL', key='listing_url')
    name = st.text_input('Exact item name / model', key='listing_name', help='Required only for exact retail comparison, not catalogue search. Imported seller titles are not verified model numbers; confirm the exact model.')
    category = st.selectbox('Listing category', CATEGORIES)
    brand = st.text_input('Listing brand')
    condition = st.multiselect('Claimed conditions', CONDITIONS, key='listing_conditions')
    description = st.text_area('Full seller description', height=160, key='listing_description')
    source_snapshot = st.session_state.get('reef_import')
    if source_snapshot and source_snapshot['lot']['url'] != url:
        source_snapshot = None
    imported_images = image_urls(source_snapshot['lot']) if source_snapshot else []
    if source_snapshot:
        st.info(f"Imported through ReefAPI at {display_time(source_snapshot['fetched_at'])}. Confirm the exact model and condition; enter actual buyer premium, taxes, shipping and fees from the sale terms. Imported titles are not retail verification.")
        show_public_photos(source_snapshot['lot'], all_photos=True)
        with st.expander('Imported auction terms and current-bid snapshot'):
            st.json(source_snapshot['lot'])
    upload_scope = fingerprint(source_snapshot) if source_snapshot else 'manual'
    uploads = st.file_uploader('Listing photos (up to 5, 10 MB each)', type=['png', 'jpg', 'jpeg', 'webp'], accept_multiple_files=True, key='uploads:' + upload_scope)
    photos = []
    if len(uploads or []) > 5:
        st.error('Upload at most five photos.')
        st.stop()
    for uploaded in uploads or []:
        raw = uploaded.getvalue()
        try:
            validate_photo(raw)
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        photos.append((Path(uploaded.name).name, raw))
    if photos:
        show_photos(photos)
    st.warning('Photos may show visible concerns. They never prove functionality, authenticity, or absence of hidden defects.')
    vision_key = fingerprint({'description': description, 'conditions': condition, 'auction_source': source_snapshot,
                              'photos': [(n, hashlib.sha256(b).hexdigest()) for n, b in photos]})
    with st.expander('Photo inspection', expanded=True):
        reviewed = st.checkbox('I reviewed the uploaded photos manually', key='reviewed:' + vision_key)
        damage = st.checkbox('Visible damage', key='damage:' + vision_key)
        missing = st.checkbox('Missing accessories', key='missing:' + vision_key)
        inconsistent = st.checkbox('Photo / description condition inconsistency', key='inconsistent:' + vision_key)
        notes = st.text_area('Photo findings, missing accessories and uncertainty', key='notes:' + vision_key)
        if setting('VISION_API_KEY'):
            consent = st.checkbox('Send these photos and seller claims to the configured OpenAI photo service')
            if st.button('Analyze photos', disabled=not (consent and photos)):
                try:
                    with st.spinner('Inspecting visible evidence…'):
                        result = vision_photos(photos, description, condition, setting('VISION_API_KEY'), setting('VISION_MODEL', 'gpt-4.1-mini'))
                    st.session_state.vision = {'key': vision_key, 'result': result}
                except Exception:
                    st.session_state.pop('vision', None)
                    st.error('Photo analysis failed. No AI assessment is available; continue manual inspection.')
        else:
            st.caption('Optional AI photo analysis needs VISION_API_KEY. Manual observations are stored as manual assessments.')
        if imported_images:
            st.caption('Imported public photos support manual review. Upload photo files above to include them in AI analysis and preserve their bytes in history.')
    vision_state = st.session_state.get('vision', {})
    vision = vision_state.get('result') if vision_state.get('key') == vision_key else None
    observations = {'reviewed': bool(reviewed and (photos or imported_images)), 'damage': damage, 'missing': missing,
                    'inconsistent': inconsistent, 'notes': notes}
    assessment = assess(description, condition, len(photos) + len(imported_images), observations, vision)
    for flag in assessment['flags']:
        st.caption('• ' + flag)

    st.subheader('Canadian retail comparison')
    retail_condition = st.selectbox('Comparable retail condition', CONDITIONS)
    source_mode = st.radio('Price source', ['Manual entry', 'Live authorized retailer API'], horizontal=True)
    retail_tax = st.number_input('Retail sales tax (%)', 0.0, 100.0, 13.0)
    source = {}
    if source_mode == 'Manual entry':
        retail_url = st.text_input('Retail comparison URL (exact matching item)')
        retailer = st.text_input('Canadian retailer name')
        retail_model = st.text_input('Retail model number')
        retail = st.number_input('Comparable price (CAD, before tax)', min_value=0.0, value=100.0, step=5.0)
        retail_delivery = st.number_input('Retail delivery cost (CAD, tax included)', min_value=0.0, value=0.0)
        manual_scope = fingerprint({'url': retail_url, 'retailer': retailer, 'retail_model': retail_model,
                                    'listing_model': name, 'condition': retail_condition, 'claims': condition,
                                    'price': retail, 'delivery': retail_delivery})
        manual_verified = st.checkbox('I checked this source: exact model, equivalent condition, in stock in Canada, price in CAD', key='manual:' + manual_scope)
        valid_manual = bool(manual_verified and https_url(retail_url) and retailer.strip()
                            and name.strip() and retail_model.strip().casefold() == name.strip().casefold()
                            and retail_condition in condition and retail > 0)
        if valid_manual and st.session_state.get('manual_check', {}).get('scope') != manual_scope:
            st.session_state.manual_check = {'scope': manual_scope, 'checked_at': utcnow()}
        source = {'kind': 'manual_verified' if valid_manual else 'manual_unverified', 'verified': valid_manual,
                  'model': retail_model, 'condition': retail_condition, 'country': 'CA', 'currency': 'CAD',
                  'price': retail, 'delivery': retail_delivery, 'url': retail_url, 'retailer': retailer,
                  'checked_at': st.session_state.manual_check['checked_at'] if valid_manual else None, 'in_stock': valid_manual}
        st.info('Manually verified by you; not a live price.' if valid_manual else 'Manually entered price — unverified.')
    else:
        retail_key = fingerprint({'model': name, 'condition': retail_condition})
        endpoint = setting('RETAIL_API_URL')
        if not endpoint:
            st.info('Configure RETAIL_API_URL for an authorized Canadian retail adapter. No scraping is performed.')
        if st.button('Verify live retail price', disabled=not (endpoint and name.strip() and retail_condition in condition)):
            st.session_state.pop('retail', None)
            try:
                with st.spinner('Checking authorized retail source…'):
                    result = live_retail(endpoint, setting('RETAIL_API_KEY'), model=name, condition=retail_condition)
                st.session_state.retail = {'key': retail_key, 'source': result}
            except Exception:
                st.error('No live price verified. Check adapter configuration and exact model / condition match.')
        live = st.session_state.get('retail', {})
        source = live.get('source', {}) if live.get('key') == retail_key else {}
        if source and not source_fresh(source):
            st.warning('Retail observation expired after 15 minutes. Verify it again.')
            source = {}
        if source:
            st.success(f"Live verified: {source['retailer']} — {money(source['price'])} CAD. Observed {display_time(source['observed_at'])}")
        retail, retail_delivery = source.get('price', 0), source.get('delivery', 0)

    # Compare multiple exact-match sources within this listing inspection.
    comparison_scope = fingerprint({'url': url, 'model': name, 'conditions': condition})
    if st.session_state.get('comparison_scope') != comparison_scope:
        st.session_state.comparison_scope = comparison_scope
        st.session_state.price_sources = []
    if st.button('Add verified source to comparison', disabled=not source.get('verified')):
        st.session_state.price_sources = [s for s in st.session_state.price_sources if s['url'] != source['url']] + [source]
    if st.session_state.price_sources and st.button('Clear comparison sources'):
        st.session_state.price_sources = []
    saved_sources = st.session_state.price_sources
    candidates = [s for s in saved_sources if s.get('kind') != 'live_verified' or source_fresh(s)]
    if source.get('verified'):
        candidates = [s for s in candidates if s['url'] != source['url']] + [source]
    if len(candidates) > 1:
        source = min(candidates, key=lambda s: s['price'] * (1 + retail_tax / 100) + s['delivery'])
        retail, retail_delivery = source['price'], source['delivery']
        st.info(f"Using lowest available delivered comparison: {source['retailer']} ({source['kind'].replace('_', ' ')}).")
        st.dataframe([{'Retailer': s['retailer'], 'Price CAD': s['price'], 'Delivery CAD': s['delivery'],
                       'Verification': s['kind'], 'URL': s['url']} for s in candidates], hide_index=True, use_container_width=True)
    elif candidates:
        source = candidates[0]
        retail, retail_delivery = source['price'], source['delivery']
        if saved_sources:
            st.info(f"Using saved comparison: {source['retailer']} — {money(source['price'])} CAD ({source['kind'].replace('_', ' ')}). Clear comparison sources to stop using it.")
    comparison_sources = candidates or ([source] if source else [])

    st.subheader('All-in bid calculation')
    a, b = st.columns(2)
    with a:
        premium = st.number_input('Buyer premium (%)', 0.0, 100.0, 15.0)
        auction_tax = st.number_input('Auction sales tax (%)', 0.0, 100.0, 13.0)
        premium_taxable = st.checkbox('Buyer premium is taxable', value=True)
        shipping = st.number_input('Mandatory shipping / pickup (CAD, before tax if taxable)', min_value=0.0, value=0.0)
        shipping_taxable = st.checkbox('Shipping / pickup is taxable')
    with b:
        fees = st.number_input('Other mandatory fees (CAD, before tax if taxable)', min_value=0.0, value=0.0)
        fees_taxable = st.checkbox('Other mandatory fees are taxable', value=True)
        current = st.number_input('Proposed hammer bid (CAD)', min_value=0.0, value=0.0, key='proposed_hammer')
        increment = st.number_input('Bid increment (CAD)', min_value=0.01, value=1.0)
    terms = st.checkbox('I checked seller terms, tax treatment, pickup radius, exact model and equivalent condition', key='terms_confirmed')
    functionality = st.checkbox('I accept the documented uncertainty; photos do not establish functionality', key='uncertainty_confirmed')
    inputs = dict(retail=retail, retail_tax=retail_tax, retail_delivery=retail_delivery,
                  premium=premium, auction_tax=auction_tax, premium_taxable=premium_taxable,
                  shipping=shipping, shipping_taxable=shipping_taxable, fees=fees,
                  fees_taxable=fees_taxable, current=current, increment=increment, discount=discount)
    calculation = calculate(**inputs)
    x, y, z = st.columns(3)
    x.metric('Retail delivered total', money(calculation['retail_total']))
    y.metric('Maximum hammer bid', money(calculation['max_hammer']))
    z.metric('Savings at proposed bid', f"{calculation['savings_percent']:.1f}%")
    st.caption((f"Auction all-in: {money(calculation['current_total'])}. Target budget: {money(calculation['budget'])}. "
                "Ceiling is floored to increment multiples; check the auction's actual bid ladder.").replace('$', r'\$'))
    watch_match = (not categories or category in categories) and (not lines(brands) or brand.casefold() in {b.casefold() for b in lines(brands)})
    watch_match = watch_match and (not lines(models) or name.casefold() in {m.casefold() for m in lines(models)})
    watch_match = watch_match and (not lines(keywords) or any(k.casefold() in (name + ' ' + description).casefold() for k in lines(keywords)))
    if not watch_match:
        st.warning("Listing does not match the active watchlist's category, brand, model or keyword preferences.")
    complete = bool(https_url(url) and name.strip() and description.strip() and condition and (photos or imported_images)
                    and terms and functionality and source.get('condition') in condition and watch_match)
    decision = decide(calculation, assessment, source, complete=complete,
                      accepted_conditions=accepted, claimed_conditions=condition)
    decision = review_imported_bid(decision, source_snapshot, current)
    getattr(st, {'eligible': 'success', 'requiring review': 'warning', 'rejected': 'error'}[decision['status']])(
        'Decision: ' + decision['status'].capitalize())
    for reason_text in decision['reasons']:
        st.write('• ' + reason_text)
    record = {'listing_url': url, 'name': name, 'category': category, 'brand': brand,
              'conditions': condition, 'description': description, 'watchlist_name': watch_name,
              'watchlist': watch_payload, 'assessment': assessment, 'price_sources': comparison_sources,
              'selected_price_source': source,
              'bid_inputs': inputs, 'calculation': {k: str(v) if not isinstance(v, bool) else v for k, v in calculation.items()},
              'checklist': {'terms': terms, 'uncertainty_accepted': functionality, 'complete': complete},
              'decision': decision, 'photo_filenames': [n for n, _ in photos],
              'photo_sha256': [hashlib.sha256(raw).hexdigest() for _, raw in photos],
              'auction_source': source_snapshot}
    if st.button('Save inspection', use_container_width=True):
        if name.strip():
            record_id = store.save_inspection(owner, record, photos)
            st.success(f'Inspection #{record_id} saved privately.')
        else:
            st.error('Enter an exact item name / model before saving.')
    if calculation['above_ceiling'] and calculation['affordable']:
        st.warning('An exception requires manual review and never submits a bid.')
        reason = st.text_area('Reason for exceeding this bid ceiling')
        approval_label = f"I explicitly approve this {money(current)} hammer bid above the {money(calculation['max_hammer'])} ceiling"
        approval = st.checkbox(approval_label.replace('$', r'\$'), key='approval:' + fingerprint(record))
        if st.button('Approve this exception and save inspection', disabled=not (approval and reason.strip() and name.strip()), use_container_width=True):
            record['decision'] = decide(calculation, assessment, source, complete=complete,
                                         accepted_conditions=accepted, claimed_conditions=condition, approved=True)
            record['decision'] = review_imported_bid(record['decision'], source_snapshot, current)
            if not record['decision']['approved']:
                st.error('This listing cannot receive a ceiling exception under the active condition preferences.')
            else:
                record['approval'] = {'owner': owner, 'reason': reason.strip(), 'approved_at': utcnow(),
                                      'scope_sha256': fingerprint(record)}
                record_id = store.save_inspection(owner, record, photos)
                st.success(f'Exception recorded in inspection #{record_id}; decision remains requiring review.')
    download_record(record, 'hibid_inspection.json')
    if https_url(url):
        st.link_button('Open listing to review / bid manually', url, use_container_width=True)

with catalogue_tab:
    st.header('Listing catalogue')
    reef_key = setting('REEFAPI_KEY')
    source_choice = st.radio('Catalogue source', ['Live HiBid listings', 'Saved inspections', 'Saved auction snapshots'], index=0 if reef_key else 1)
    if source_choice == 'Live HiBid listings':
        render_live_catalogue(reef_key, store, owner, postal, radius, display_time)
    elif source_choice == 'Saved auction snapshots':
        render_saved_auction_snapshots(store, owner, display_time)
    else:
        st.caption('Your saved auction inspections — not live auction search results. Prices, availability and condition may have changed since inspection.')
        query = st.text_input('Search catalogue (optional)', help='Browse everything or enter a brand, category, model or keyword. No specific item is required.')
        filter_category = st.selectbox('Catalogue category', ['All categories'] + CATEGORIES)
        filter_status = st.selectbox('Catalogue decision', ['All decisions', 'eligible', 'requiring review', 'rejected'])
        catalogue = catalogue_entries(store.history(owner), query, filter_category, filter_status)
        st.caption(f'{len(catalogue)} matching listings from the latest 200 inspection snapshots. Only the latest saved inspection per listing URL is shown.')
        if not catalogue:
            st.info('No saved listings match yet. Add a listing in Inspect listing, review it, then click Save inspection. Choose Live HiBid listings to connect ReefAPI.')
        else:
            per_page = 10
            pages = (len(catalogue) + per_page - 1) // per_page
            page_number = st.selectbox('Catalogue page', list(range(1, pages + 1)))
            page_entries = catalogue[(page_number - 1) * per_page:page_number * per_page]
            for row in range(0, len(page_entries), 2):
                columns = st.columns(2)
                for column, entry in zip(columns, page_entries[row:row + 2]):
                    with column:
                        with st.container(border=True):
                            st.subheader(entry.get('name', 'Unnamed listing'))
                            st.caption(f"{entry.get('category', 'Category not recorded')} · {entry.get('brand') or 'Brand not recorded'}")
                            st.caption(f"Saved {display_time(entry['created_at'])} · " + ('ReefAPI auction snapshot, not refreshed now' if entry.get('auction_source') else 'Manually entered auction information'))
                            saved_photos = store.photos(owner, entry['id'])
                            if saved_photos:
                                show_photos(saved_photos[:1])
                            elif entry.get('auction_source'):
                                show_public_photos(entry['auction_source']['lot'])
                            else:
                                st.caption('No listing photo saved.')
                            st.write('Claimed condition: ' + (', '.join(entry.get('conditions', [])) or 'Not recorded'))
                            decision = entry.get('decision', {})
                            getattr(st, {'eligible': 'success', 'rejected': 'error'}.get(decision.get('status'), 'warning'))(
                                'Decision: ' + decision.get('status', 'requiring review').capitalize())
                            calculation = entry.get('calculation', {})
                            st.text('Proposed hammer bid: ' + amount(entry.get('bid_inputs', {}).get('current')))
                            st.text('Auction all-in estimate: ' + amount(calculation.get('current_total')))
                            st.text('Maximum hammer bid: ' + amount(calculation.get('max_hammer')))
                            st.text('Retail delivered comparison: ' + amount(calculation.get('retail_total')))
                            st.text('Savings at proposed bid: ' + percentage(calculation.get('savings_percent')))
                            source = entry.get('selected_price_source') or next(iter(entry.get('price_sources', [])), {})
                            st.caption('Retail evidence: ' + {'live_verified': 'Live verified at inspection time; not refreshed now',
                                                             'manual_verified': 'Manually verified at inspection time',
                                                             'manual_unverified': 'Manual price — unverified'}.get(source.get('kind'), 'Not recorded'))
                            if source.get('retailer'):
                                st.text('Retailer: ' + source['retailer'])
                            with st.expander('All listing details and inspection findings'):
                                st.text(entry.get('description') or 'No description recorded.')
                                for flag in entry.get('assessment', {}).get('flags', []):
                                    st.write('• ' + flag)
                                st.text('Photo notes: ' + (entry.get('assessment', {}).get('observations', {}).get('notes') or 'Not recorded'))
                                st.warning('Photos are never proof of functionality. Saved eligibility is not a current auction or bidding guarantee.')
                                for reason in decision.get('reasons', []):
                                    st.write('• ' + reason)
                                if decision.get('approved'):
                                    st.warning('An explicit ceiling exception was recorded for this saved inspection only.')
                                st.json(entry)
                                download_record(entry, f"inspection_{entry['id']}.json")
                            if https_url(entry.get('listing_url', '')):
                                st.link_button('Review current auction listing manually', entry['listing_url'], use_container_width=True)
                            if https_url(source.get('url', '')):
                                st.link_button('Open retail comparison', source['url'], use_container_width=True)

with history_tab:
    st.header('Private inspection history')
    st.caption('Latest 200 saved inspections. Each save is an immutable snapshot, including photos and any approval.')
    history = store.history(owner)
    if not history:
        st.info('No inspections saved yet.')
    if history:
        by_id = {entry['id']: entry for entry in history}
        chosen = st.selectbox('Saved inspection', list(by_id), format_func=lambda i: f"#{i} · {by_id[i]['name']} · {by_id[i]['decision']['status']}")
        entry = by_id[chosen]
        with st.expander(f"#{entry['id']} · {entry['name']} · {entry['decision']['status']} · {display_time(entry['created_at'])}"):
            st.json(entry)
            saved_photos = store.photos(owner, entry['id'])
            if saved_photos:
                show_photos(saved_photos)
            download_record(entry, f"inspection_{entry['id']}.json")

with agents_tab:
    from dealhunter.operations import render_operations
    render_operations(store, owner, setting('REEFAPI_KEY'))

st.caption('Not affiliated with HiBid. No HiBid credentials are collected or stored. Eligibility is decision support, not authorization to bid.')
