"""Authenticated phone inbox for durable specialist-agent workflows."""
import json
import streamlit as st

from dealhunter.agents import enqueue_team, run_next_job


def render_operations(store, owner, reef_key=''):
    st.header('Agent team')
    st.caption('Private jobs and exact-action approvals. Refresh this inbox on your phone to see completed work.')
    st.info('Scout: ReefAPI search when configured. Inspection: saved findings and description signals. Market research: your supplied comparable evidence. Bid assistant and ad writer: proposals only.')
    st.warning('Live local sold-listing research, HiBid bidding and personal Facebook Marketplace/Kijiji publishing are not connected. Approval records intent; it does not submit a bid or publish an ad. No external action adapters are installed.')
    watches = store.watchlists(owner)
    if watches:
        watch_index = st.selectbox('Scout watchlist', range(len(watches)), format_func=lambda i: watches[i]['name'])
        categories = st.text_input('Provider category IDs for scout (comma separated)', help='Use ReefAPI category IDs; category names are not automatically resolved.')
        st.caption('Up to three model/brand/keyword queries, ten reserved search calls per user per Toronto day. Category and condition matching still need verification.')
        if st.button('Queue product scout', disabled=not bool(reef_key), use_container_width=True):
            ids = [part.strip() for part in categories.split(',') if part.strip()]
            if any(not part.isascii() or not part.isdigit() or int(part) <= 0 for part in ids) or len(ids) > 5:
                st.error('Enter at most five numeric provider category IDs.')
            elif watches[watch_index].get('categories') and not ids:
                st.error('Supply category IDs for this categorized watchlist before scouting.')
            else:
                store.enqueue_agent_job(owner, 'Scout', {'watchlist': watches[watch_index], 'category_ids': list(dict.fromkeys(int(part) for part in ids))})
                st.success('Scout queued. Run a queued agent below, or use the worker.')
    else:
        st.caption('Save a watchlist in the sidebar to queue a product scout.')

    history = store.history(owner)
    if history:
        with st.expander('Prepare inspection, resale research and both ad drafts'):
            by_id = {entry['id']: entry for entry in history}
            chosen = st.selectbox('Agent inspection', list(by_id), format_func=lambda i: f"#{i} · {by_id[i]['name']}")
            st.caption('Exact model and equivalent condition must match the saved inspection. Supply recent CAD evidence measured from its postal code, within 50 km. No live market feed is connected.')
            upload = st.file_uploader('Comparable evidence JSON (up to 1 MB)', type=['json'], key='agent_comparables')
            st.code('[{"platform":"Kijiji", "url":"https://www.kijiji.ca/v-category/city/1234567890", "model":"EXACT MODEL", "condition":"Used", "currency":"CAD", "centre_postal":"L7A 4X1", "distance_km":10, "status":"sold", "sale_verified":true, "sale_evidence":"Your evidence of actual price and sale", "sold_price":100, "listed_at":"ISO timestamp with timezone", "sold_at":"ISO timestamp with timezone", "observed_at":"ISO timestamp with timezone"}]', language='json')
            st.caption('At least three genuine completed sales are required for price/duration estimates. Active records use asking_price and status active; they never count as completed sales. Remove the example and supply your own evidence.')
            asking = st.number_input('Ad asking price (CAD)', min_value=0.01, value=100.0, key='agent_asking')
            costs = st.number_input('Total additional resale costs (CAD)', min_value=0.0, value=0.0, help='Include repairs, cleaning, platform fees, transport and other resale costs beyond the auction all-in estimate.')
            profit = st.number_input('Minimum conservative resale profit (CAD)', min_value=0.0, value=20.0)
            costs_confirmed = st.checkbox('I included all expected additional resale costs')
            owned = st.checkbox('I own and have received this item')
            rights = st.checkbox('The saved inspection photos are mine or licensed for my resale ads')
            facts = st.text_area('Seller-confirmed facts for the ad', max_chars=6000)
            if st.button('Queue research and both marketplace drafts', use_container_width=True):
                try:
                    records = []
                    if upload is not None:
                        if upload.size > 1024 * 1024:
                            raise ValueError('Comparable upload exceeds 1 MB')
                        records = json.loads(upload.getvalue())
                        if not isinstance(records, list) or len(records) > 2000:
                            raise ValueError('Upload a JSON array of at most 2,000 records')
                    ids = enqueue_team(store, owner, by_id[chosen], records, asking_price=asking,
                                       owned=owned, photos_authorized=rights, confirmed_details=facts,
                                       resale_costs=costs, minimum_profit=profit, resale_costs_confirmed=costs_confirmed)
                    st.success('Queued jobs: ' + ', '.join(map(str, ids)))
                except (ValueError, UnicodeError):
                    st.error('Provide a valid JSON array within the upload limits.')
    else:
        st.caption('Save an inspection first to prepare research, a bid proposal and marketplace drafts.')

    if st.button('Run next queued agent', use_container_width=True):
        identifier = run_next_job(store, owner, reef_key)
        st.info(f'Processed job #{identifier}. See its outcome below.' if identifier else 'No runnable jobs.')
    st.button('Refresh agent inbox', use_container_width=True)
    for job in store.agent_jobs(owner):
        with st.expander(f"Job #{job['id']} · {job['agent']} · {job['status']}"):
            if job.get('error'):
                st.error(job['error'])
            if job.get('result'):
                st.json(job['result'])
    with st.expander('Slack communication and delivery status'):
        st.caption('The optional Slack Socket Mode worker uses a configured private channel. Approval records intent only. Slack notes do not change job instructions.')
        delivery = store.slack_delivery_status(owner)
        if delivery:
            st.json(delivery)
            st.caption('Unknown or sending deliveries require operator review; they are not automatically resent. Use /deals status in Slack to request current information.')
        else:
            st.caption('No Slack notifications recorded. Configure the separate listener to enable delivery.')
        for note in store.agent_notes(owner):
            st.text(f"Note #{note['id']} · {note['created_at']} · {note['source']}")
            st.text(note['content'])
    st.subheader('Exact-action approval inbox')
    actions = store.agent_actions(owner)
    if not actions:
        st.caption('No proposed actions. Insufficient market evidence blocks resale bid recommendations.')
    for action in actions:
        payload = action['payload']
        identifier = action['id']
        prefix = f"agent_action_{identifier}_{action['fingerprint']}"
        with st.expander(f"Action #{identifier} · {payload.get('platform', 'HiBid bid')} · {action['status']}"):
            if payload['kind'] == 'publish_ad':
                st.text(payload['title'])
                st.code(payload['body'], language=None, wrap_lines=True)
            st.json(payload)
            st.caption(f"Approval expires: {action.get('expires_at') or 'not approved'}. No external submission has occurred through this dashboard.")
            if action['status'] == 'proposed':
                checked = st.checkbox('I approve this exact item, amounts, text, photos and platform shown above', key=prefix + '_confirm')
                reason = st.text_input('Approval reason', key=prefix + '_reason')
                if st.button('Record exact-action approval', key=prefix + '_approve', disabled=not checked or not reason.strip()):
                    try:
                        store.approve_agent_action(owner, identifier, action['fingerprint'], reason)
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
            if action['status'] in ('proposed', 'approved'):
                if st.button('Revoke action', key=prefix + '_revoke'):
                    store.revoke_agent_action(owner, identifier)
                    st.rerun()
            st.button('Submit through permitted integration — unavailable', key=prefix + '_submit', disabled=True)
