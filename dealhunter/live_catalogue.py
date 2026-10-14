"""Authenticated, explicitly refreshed ReefAPI catalogue UI."""
import hashlib
import html
import json
from datetime import datetime, timezone

import streamlit as st

from dealhunter.reefapi import ReefAPI, ReefAPIError, image_urls, numeric, radius_miles


def query_scope(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def auction_amount(value, currency):
    number = numeric(value)
    if number is None:
        return "Not reported"
    label = currency if isinstance(currency, str) and currency.isalpha() and len(currency) == 3 else "Currency unknown"
    return f"{label} {number:,.2f}"


def closing_time(value, display_time):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return 'Timezone not reported; confirm with auction house'
        return display_time(value)
    except (AttributeError, ValueError, TypeError):
        return 'Not reported in a recognized timestamp format'


def show_public_photos(lot, all_photos=False):
    urls = image_urls(lot)
    if not urls:
        st.caption('No supported public photo URL reported.')
    for url in urls if all_photos else urls[:1]:
        escaped = html.escape(url, quote=True)
        st.markdown(f'<img alt="Public auction listing photo" src="{escaped}" loading="lazy" referrerpolicy="no-referrer" style="max-width:100%;max-height:320px;object-fit:contain">', unsafe_allow_html=True)


def pending_inspection(lot, fetched_at):
    # Button callbacks run before the next script execution, before widget initialization.
    if lot.get('currency') != 'CAD' or lot.get('is_closed') is True:
        st.error('Only CAD lots not reported closed can be imported into the CAD calculator.')
        return
    st.session_state.reef_pending_inspection = {'lot': lot, 'fetched_at': fetched_at}


def render_lots(lots, fetched_at, *, store, owner, display_time, client=None, scope='saved'):
    details = st.session_state.setdefault('reef_details', {})
    for start in range(0, len(lots), 2):
        for column, original in zip(st.columns(2), lots[start:start + 2]):
            identifier = original['lot_id']
            detail_key = scope + ':' + identifier
            detailed = details.get(detail_key)
            lot = detailed['lot'] if detailed else original
            observed = detailed['fetched_at'] if detailed else fetched_at
            with column:
                with st.container(border=True):
                    st.subheader(lot['title'])
                    st.caption(f"Lot {identifier} · Fetched {display_time(observed)} through ReefAPI")
                    show_public_photos(lot)
                    currency = lot.get('currency', '')
                    st.text('Current bid at fetch: ' + auction_amount(lot.get('current_bid'), currency))
                    st.text('Next bid required at fetch: ' + auction_amount(lot.get('next_bid_amount'), currency))
                    st.text('Bid count: ' + str(lot.get('bid_count') if lot.get('bid_count') is not None else 'Not reported'))
                    st.text('Closing time: ' + closing_time(lot.get('closes_at'), display_time))
                    if lot.get('is_closed') is True:
                        st.error('Auction closed at fetch — rejected for bidding.')
                    else:
                        st.warning('Requires inspection and retail comparison. No verified 70% bid ceiling yet.')
                    if currency != 'CAD':
                        st.warning('Currency is not confirmed as CAD. No currency conversion or CAD bid calculation is applied.')
                    house = lot.get('auction_house', {})
                    auction = lot.get('auction', {})
                    st.text('Auction house: ' + str(house.get('name') or 'Not reported'))
                    st.text('Location: ' + (', '.join(str(house.get(k)) for k in ('city', 'town', 'state', 'country') if house.get(k)) or 'Not reported'))
                    st.text('Sale: ' + str(auction.get('name') or 'Not reported'))
                    if client and st.button('Fetch full lot details (1 credit)', key='reef-detail:' + detail_key, use_container_width=True):
                        try:
                            with st.spinner('Fetching sale terms and all photos…'):
                                result = client.detail(identifier)
                            details[detail_key] = result
                            st.rerun()
                        except ReefAPIError as exc:
                            st.error(str(exc))
                    with st.expander('Description, photos, sale terms and all returned details'):
                        st.text(lot.get('description') or 'No description reported.')
                        show_public_photos(lot, all_photos=True)
                        st.warning('Photos never prove functionality, authenticity or absence of hidden defects.')
                        if not detailed and client:
                            st.caption('Fetch full details above for every picture, category path and complete sale terms. No automatic per-card API requests are made.')
                        st.json(lot)
                    st.link_button('Review / bid manually on HiBid', lot['url'], use_container_width=True)
                    if st.button('Save auction snapshot', key='reef-save:' + detail_key, use_container_width=True):
                        saved = store.save_auction_snapshot(owner, lot, observed)
                        st.success(f'Auction snapshot #{saved} saved privately. It is not a bid approval.')
                    st.button('Inspect this lot', key='reef-inspect:' + detail_key,
                              disabled=currency != 'CAD' or lot.get('is_closed') is True,
                              on_click=pending_inspection, args=(lot, observed), use_container_width=True)
                    if st.session_state.get('reef_import', {}).get('lot', {}).get('lot_id') == identifier:
                        st.success('Loaded for inspection. Open the Inspect listing tab and verify the model, condition and costs.')


def render_live_catalogue(api_key, store, owner, postal, radius, display_time):
    st.subheader('Current HiBid listings through ReefAPI')
    st.caption('Optional search: browse Canadian lots without entering a specific model. Bids are snapshots at fetch time, not continuously monitored.')
    if not api_key:
        st.info('Add REEFAPI_KEY in Streamlit Secrets or the hosting environment to connect live listings. Use your ReefAPI key, not a HiBid password. Saved inspections remain available in the catalogue source selector.')
        return
    try:
        client = ReefAPI(api_key)
    except ReefAPIError:
        st.error('REEFAPI_KEY must be a nonempty string configured securely in Streamlit Secrets or the hosting environment.')
        return
    query = st.text_input('Live search (optional)', max_chars=500, help='Brand, model or keywords; leave blank to browse nearby lots.')
    province = st.selectbox('Canadian province / territory', ['', 'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE', 'QC', 'SK', 'YT'], index=9)
    postcode = st.text_input('Live search postcode (optional)', value=postal)
    distance = st.number_input('Live pickup radius (km)', min_value=1, max_value=500, value=int(radius))
    sort = st.selectbox('Live results order', ['ending_soonest', 'newest', 'bid_low_first', 'bid_high_first', 'lot_number'])
    status = st.selectbox('Live auction status', ['open', 'closing', 'all', 'closed', 'hot', 'featured', 'top'])
    if st.button('Load HiBid categories (1 credit)'):
        try:
            loaded = client.categories()
            st.session_state.reef_categories = loaded['categories']
        except ReefAPIError as exc:
            st.error(str(exc))
    provider_categories = st.session_state.get('reef_categories', {})
    category_ids = st.multiselect('HiBid categories (optional, up to 5)', sorted(provider_categories, key=lambda c: provider_categories[c]),
                                  format_func=lambda c: provider_categories[c])
    page = st.number_input('Live results page', min_value=1, max_value=1000, value=1)
    size = st.selectbox('Results per category', [10, 25, 50, 100])
    parameters = dict(query=query, postcode=postcode, km=distance, province=province,
                      category_ids=category_ids, page=int(page), max_results=size, sort=sort, status=status)
    scope = query_scope(parameters)
    cost = max(1, len(category_ids))
    st.caption(f'Canada filter applied. {radius_miles(distance)} miles sent to the API when a postcode is supplied. Search makes {cost} request(s), approximately {cost} credit(s); details and category loading are separate calls.')
    if st.button('Search / refresh current listings', use_container_width=True, disabled=len(category_ids) > 5):
        st.session_state.pop('reef_search', None)
        st.session_state.reef_details = {}
        try:
            with st.spinner('Searching the current HiBid catalogue…'):
                result = client.search(**parameters)
            st.session_state.reef_search = {'scope': scope, 'result': result}
        except ReefAPIError as exc:
            st.error(str(exc))
            st.caption('No current result is presented for this failed refresh. Earlier calls in a multi-category search may already have consumed credits.')
    state = st.session_state.get('reef_search', {})
    if state.get('scope') != scope:
        st.info('Click Search / refresh current listings to load these filters. No exact item is required.')
        return
    result = state['result']
    fetched = datetime.fromisoformat(result['fetched_at'])
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    st.caption(f"{len(result['results'])} unique lots on requested page {result['page']} · Fetched {display_time(result['fetched_at'])}")
    if age > 300:
        st.warning('Results are over five minutes old. Refresh before relying on bids or closing times; no automatic calls are made.')
    if len(category_ids) > 1:
        st.caption('This page is fetched separately for each category and duplicates are removed. It is not a global combined ranking or unique total count.')
    with st.expander('Provider search totals and credit reports'):
        st.json(result['reports'])
    if not result['results']:
        st.info('The provider returned no matching lots for this page. Broaden filters or change page; no substitute results were invented.')
    display_page = st.selectbox('Fetched cards page', list(range(1, max(1, (len(result['results']) + 9) // 10) + 1)))
    visible = result['results'][(display_page - 1) * 10:display_page * 10]
    render_lots(visible, result['fetched_at'], store=store, owner=owner,
                display_time=display_time, client=client, scope=scope)


def render_saved_auction_snapshots(store, owner, display_time):
    st.caption('Saved ReefAPI auction snapshots — historical data, not refreshed live.')
    snapshots = store.auction_snapshots(owner)
    if not snapshots:
        st.info('No auction snapshots saved yet.')
        return
    selected = st.selectbox('Saved auction snapshot', list(range(len(snapshots))),
                            format_func=lambda i: f"#{snapshots[i]['id']} · {snapshots[i]['lot']['title']}")
    snapshot = snapshots[selected]
    render_lots([snapshot['lot']], snapshot['fetched_at'], store=store, owner=owner, display_time=display_time,
                scope='saved:' + str(snapshot['id']))
