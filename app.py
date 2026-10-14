import streamlit as st
import json, datetime, os
from decimal import Decimal
st.set_page_config(page_title='HiBid Smart Deal Hunter',layout='wide')
st.title('HiBid Smart Deal Hunter')
st.caption('Local decision assistant • No automated scraping, login, or bidding')
with st.sidebar:
 st.header('Agent settings')
 postal=st.text_input('Search centre','L7A 4X1')
 radius=st.number_input('Pickup radius (km)',1,500,50)
 discount=st.slider('Required discount (%)',0,95,70)
 categories=st.multiselect('Categories',['Toys & Games','Electronics','Power Tools','Home Appliances','Furniture & Décor','Lawn & Garden','Automotive','Computers','Sports & Outdoors','Other'],default=['Toys & Games','Electronics','Power Tools'])
 watch=st.text_area('Specific items / model numbers (one per line)')
 st.info('Use HiBid filters manually to find lots. Paste listing details below for inspection.')
st.header('Inspect an auction listing')
url=st.text_input('HiBid listing URL')
name=st.text_input('Exact item name / model')
condition=st.multiselect('Claimed conditions',['Factory sealed','New open-box','Like new','Used excellent','Used good','Refurbished','Display model','Customer return','Damaged packaging','For parts / not working'])
description=st.text_area('Full seller description',height=160)
photos=st.file_uploader('Upload listing photos for your own inspection record',type=['png','jpg','jpeg','webp'],accept_multiple_files=True)
if photos:
 st.image([p.getvalue() for p in photos],width=180)
 st.warning('Photos are displayed for manual review. This local v1 does not perform AI visual analysis or verify functionality.')
retail_url=st.text_input('Retail comparison URL (exact matching item)')
a,b,c=st.columns(3)
with a: retail=st.number_input('Lowest verified comparable price (CAD, before tax)',min_value=0.0,value=100.0,step=5.0)
with b: premium=st.number_input('Buyer premium (%)',0.0,100.0,15.0)
with c: tax=st.number_input('Applicable sales tax (%)',0.0,30.0,13.0)
d,e,f=st.columns(3)
with d: shipping=st.number_input('Mandatory shipping/pickup fees (CAD)',0.0,value=0.0)
with e: current=st.number_input('Current hammer bid (CAD)',0.0,value=0.0)
with f: increments=st.number_input('Bid increment (CAD)',0.01,value=1.0)
retail_delivery=st.number_input('Retail delivery cost (CAD)',min_value=0.0,value=0.0)
retail_total=retail*(1+tax/100)+retail_delivery
ceiling=retail_total*(1-discount/100)
max_hammer=max(0,(ceiling-shipping)/((1+premium/100)*(1+tax/100)))
max_hammer=(int((max_hammer+1e-9)/increments))*increments
current_total=current*(1+premium/100)*(1+tax/100)+shipping
savings=100*(1-current_total/retail_total) if retail_total else 0
st.divider()
x,y,z=st.columns(3)
x.metric('Retail delivered total',f'${retail_total:.2f}')
y.metric('Maximum hammer bid',f'${max_hammer:.2f}')
z.metric('Savings at current bid',f'{savings:.1f}%')
flags=[]
keywords=['untested','as is','as-is','no returns','missing','damaged','broken','not working','parts only','unknown condition','stock photo','open box','refurbished']
for k in keywords:
 if k in description.lower(): flags.append(k)
if not retail_url: flags.append('retail source not provided')
if not name: flags.append('model not identified')
if not description: flags.append('description missing')
if not photos: flags.append('photos not supplied')
if flags: st.warning('Manual review flags: '+', '.join(flags))
if current_total<=ceiling and retail_total>0:
 st.success('Price meets the discount target, subject to manual verification and bidding rules.')
else: st.error('Price does not meet the discount target. Approval required to exceed ceiling.')
st.write('**Inspection checklist:** exact model matches; packaging and seals visible; accessories present; no description/photo contradictions; seller terms checked; location within radius; retail listing in stock; condition comparable.')
notes=st.text_area('Photo inspection findings / missing accessories / pickup notes')
record={'saved_at':datetime.datetime.now().isoformat(timespec='seconds'),'listing_url':url,'name':name,'condition':condition,'description':description,'retail_url':retail_url,'retail_before_tax':retail,'retail_delivered_total':round(retail_total,2),'max_hammer_bid':round(max_hammer,2),'current_bid':current,'current_all_in':round(current_total,2),'savings_percent':round(savings,2),'flags':flags,'notes':notes,'categories':categories,'specific_watchlist':watch.splitlines(),'postal':postal,'radius_km':radius,'required_discount_percent':discount,'photo_filenames':[p.name for p in photos or []]}
st.download_button('Download inspection report (JSON)',json.dumps(record,indent=2),file_name='hibid_inspection.json',mime='application/json')
if url: st.link_button('Open HiBid listing to review / bid manually',url)
st.caption('Not affiliated with HiBid. Prices are user-entered, not live verified. Do not enter credentials here. A maximum bid is not an authorization to bid.')
