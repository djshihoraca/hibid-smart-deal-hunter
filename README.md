# HiBid Smart Deal Hunter

A private, manually operated Canadian auction inspection dashboard. It stores watchlists and inspection history, identifies condition concerns, compares retail evidence, and calculates a hammer-bid ceiling for **at least 70% all-in savings**. It never logs in to HiBid, scrapes auctions, monitors listings, or places bids.

## Run locally

Python 3.10+ is required (validated with Python 3.12).

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m dealhunter.security
```

The last command asks for a **new application password**, then prints its salted PBKDF2-SHA256 hash (600,000 rounds). Do not reuse a HiBid password. Configure accounts using environment variables or an ignored `.streamlit/secrets.toml`:

```toml
DEAL_HUNTER_USERS = '{"alice":"<generated hash>","bob":"<another generated hash>"}'
# Optional: use an absolute path on a private persistent volume.
DEAL_HUNTER_DB = "/private/persistent/dealhunter.sqlite3"
```

Then run:

```sh
streamlit run app.py --server.address=127.0.0.1 --server.headless=true --browser.gatherUsageStats=false
```

No accounts configured means the application stays locked and does not open the private database. Each configured username owns its own watchlists, history, photos and decisions. Use a unique account per person. Sign out clears session state; idle sessions expire after 30 minutes on the next interaction. Changing/removing an account hash revokes its active sessions on their next interaction. The in-process login limiter permits five attempts per username per minute and 100 total attempts per minute. Multiple app processes require a shared authentication gateway/rate limiter. Use HTTPS and access controls before exposing this application remotely; do not expose a development server directly to the Internet. Android users can open the authenticated HTTPS dashboard in their browser; columns stack on narrow screens, sidebar settings remain available, and actions have touch-sized controls.

## Workflow

1. Save named watchlists with multiple categories, brands, exact models, keywords and acceptable conditions. Brand/model filters match any saved value; keywords match any saved phrase; the groups combine with AND. Search auctions yourself within the saved postal centre and pickup radius.
2. Paste a listing URL, exact model and full seller description. Select all claimed conditions. Upload up to five PNG/JPEG/WebP photos (10 MB and 20 megapixels each). Description rules flag damage, missing accessories, uncertainty and conflicting sealed claims; they are conservative signals, not factual findings. Review the photos and record visible damage, missing accessories, inconsistent claims and notes. Review selections reset when the photo evidence, description or conditions change.
3. Enter a comparable Canadian retailer's exact model, equivalent condition, availability, price in CAD, URL and delivery cost. A manual price is labelled **unverified** until you explicitly attest to the source checks. A manually verified price stays labelled **manual**, never **live**. Attestation resets when its product, claims, source or amount changes. Alternatively use the optional authorized retail API below. Add verified sources to the comparison; the lowest delivered comparable price is selected. Clear comparison sources to remove old evidence. Saved manual checks remain historical evidence and need rechecking by the user; live observations expire after 15 minutes. Comparison lists are scoped to the listing/model/conditions and included in the saved inspection.
4. Enter buyer premium, separate retail/auction tax rates, shipping/pickup and all mandatory fixed fees. Choose whether premium, shipping and fees are taxable. Enter the proposed hammer bid and auction bid increment. Retail delivery is entered tax-inclusive. No distance, stock, seller tax treatment or bid ladder is automatically verified: complete the manual checklist.
5. Read the **eligible**, **requiring review**, or **rejected** decision. Eligibility requires a verified comparable source, matching watchlist, completed manual checklist, reviewed photos, an acceptable condition, no flagged concerns, and a feasible bid meeting the savings rule. Missing evidence and uncertainty require review; unacceptable conditions, infeasible budgets and unapproved over-ceiling bids are rejected.
6. To exceed the ceiling, enter a reason, check the confirmation for the specific bid and click **Approve this exception and save inspection**. The approval stores its user, time and the SHA-256 scope of the exact record including photo hashes. Changed inputs require another confirmation and explicit save; approvals are never carried forward. An approved exception remains **requiring review** and never submits a bid. Bids exceeding the all-in budget fail the discount rule; a fractional bid above the rounded increment ceiling also needs approval even if it fits the budget. Unacceptable watchlist conditions and infeasible budgets cannot be overridden.
7. Save inspection snapshots and view your private history. Listing details, watchlist preferences, assessments, selected/all comparable sources, tax assumptions, calculations, photos and decisions persist in SQLite. History displays the latest 200 records; older records remain stored. JSON exports include photo filenames/hashes, not photo bytes. Photos and exports are embedded in the authenticated session instead of public Streamlit media endpoints.

Photos cannot establish functionality, hidden defects, authenticity or factory sealing. An eligible decision is not a guarantee and is not authorization to bid. Check auction terms and place any bid yourself on HiBid.

## Calculation

With auction tax `t` and premium rate `p`, the hammer multiplier is `1 + t + p × (1 + t)` when premium is taxable, or `1 + t + p` otherwise. Fixed shipping and mandatory fees each receive tax only if marked taxable. Retail delivered total is `retail × (1 + retail tax) + tax-inclusive delivery`. The allowed auction budget is `retail delivered total × (1 − savings target)`. Subtract fixed costs, divide by the hammer multiplier, then floor using Decimal arithmetic to the selected bid increment. If fixed costs exceed the budget or retail is zero, there is no feasible qualifying bid (a displayed zero ceiling does not imply eligibility). Default CAD $100 retail, 13% taxes, 15% taxable premium and no fees yields a $26 hammer ceiling. Increment multiples may differ from the auction's actual bid ladder. Percentage surcharges beyond the buyer premium, tiered taxes and unusual tax treatment need separate calculation.

## Optional photo analysis

```toml
VISION_API_KEY = "<enter securely; never commit>"
VISION_MODEL = "gpt-4.1-mini"
```

Clicking **Analyze photos** additionally requires explicit consent to send the photos and seller claims to OpenAI's HTTPS Chat Completions API (`api.openai.com`). Images are downscaled and metadata stripped before transmission. Results are labelled unverified AI observations; any finding requires manual review. No functionality inference is accepted. Invalid responses/failures do not yield a verified assessment, and stale results are invalidated when evidence changes. API usage may incur costs and is subject to provider data policies. Without a key, manual inspection remains available. Keys are read only from environment/secrets, never persisted with inspection records.

## Optional live Canadian retail verification

No retailer scraping is included. A deployment administrator can configure an **authorized retailer API adapter** using `RETAIL_API_URL` (HTTPS, trusted endpoint) and optional `RETAIL_API_KEY`. Use only endpoints the administrator trusts and is authorized to call; end users cannot supply request destinations. Redirects are disabled and TLS verification stays enabled. The adapter is responsible for authoritative retail evidence and must not fabricate observations. The request is a POST with this JSON:

```json
{"model":"ABC123","condition":"Like new","country":"CA","currency":"CAD"}
```

The optional key is sent as a Bearer header. A successful response must contain:

```json
{
  "model": "ABC123", "condition": "Like new", "country": "CA", "currency": "CAD",
  "in_stock": true, "price": 199.99, "delivery": 0,
  "url": "https://authorized-retailer.example/product/ABC123",
  "retailer": "Canadian retailer", "observed_at": "2026-10-14T16:00:00Z"
}
```

Use the actual observation time with a timezone (the example expires). `price` is before tax and `delivery` is tax-inclusive CAD. Exact model/condition, CA, CAD, in-stock status, positive finite price, nonnegative delivery and observation age under 15 minutes are enforced before assigning **live verified**. This verifies the adapter's current observation, not all Canadian retailers or a universal lowest price. No adapter/key is bundled and external providers are not needed for the manual workflow. Allow the adapter's hostname in deployment networking; keep its credentials out of URLs, logs and source files.

## Data and security

The default database is `data/dealhunter.sqlite3`, ignored by Git and created with owner-only file permissions. Mount a persistent private volume for durable hosted storage; ephemeral deployment filesystems lose history. Back up the database consistently (SQLite backup API or stop the app before copying). The database is not encrypted at rest: host filesystem access is an administrator privilege. There is no HiBid account/password field and no credential database. Uploaded files are validated as images and never executed. User data is scoped with parameterized queries; no private records are accessed before authentication. User-supplied HTML is not rendered. Do not commit `.streamlit/secrets.toml`, database files or keys. Do not serve the database directory as static content. Internet-facing deployments should additionally use an authenticated HTTPS gateway for every path, especially with multiple worker processes.

## Tests

```sh
python -m pytest -q
python -m pip check
```

Tests exercise tax combinations and the 70% boundary, invalid inputs, impossible budgets, description conflicts, photo uncertainty, tenant isolation, persistence, password verification, login/logout, watchlist saving, manual verification resets, and snapshot-bound ceiling approvals. API tests mock provider responses and enforce source matching/freshness, safe redirects and photo response schemas. Streamlit AppTest runs the actual dashboard; uploaded image bytes are injected for the photo workflow. Live paid photo analysis and retailer calls require credentials and are not exercised by the offline suite. Narrow-screen CSS is included; physical Android browser testing remains recommended before remote deployment.
