# HiBid Smart Deal Hunter v1

A local, manually populated auction inspection and bid-ceiling calculator. It does **not** scrape HiBid, access accounts, verify retail prices, perform AI image recognition, monitor auctions, or submit bids.

## Start

1. Install Python 3.10+.
2. In this folder run `pip install -r requirements.txt`.
3. Run `streamlit run app.py`.
4. Open the local address shown in your terminal.

## Workflow

Search HiBid manually for items within 50 km of L7A 4X1. Paste the listing URL, exact model and full description; attach screenshots/photos. Verify the cheapest *available* Canadian retailer listing for the same model and equivalent condition. Enter its price and URL. Enter the actual auction buyer premium, tax, fees and bid increment. Inspect images manually and record concerns. The calculator estimates the maximum hammer bid needed for 70% all-in savings, and lets you download an inspection record. Place bids yourself on HiBid only after checking seller terms.

## Notes

The calculator assumes buyer premium is taxable and retail and auction share the entered sales-tax rate. Confirm real auction terms, shipping, condition and tax treatment; the estimate may differ. It floors bids to multiples of the chosen increment, which may not match the auction's actual bid ladder. For unusual taxes or fees, calculate separately. Never provide passwords to this app.
