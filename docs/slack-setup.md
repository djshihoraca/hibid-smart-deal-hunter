# Slack private-channel alerts and approvals

The optional Slack listener connects outbound using Socket Mode. No public callback server, port forwarding, Slack signing-secret endpoint, or incoming webhook is needed. It runs separately from Streamlit and the agent worker, against the **same private persistent SQLite database**. It does not execute bids or publish ads.

## 1. Create a dedicated private channel

In your own Slack workspace, create a private channel such as `hibid-deals`. Only invite people trusted to read the listing, pricing, draft and inspection information. Only the explicitly configured user can approve actions, even if other members can read the channel. Slack administrators and Slack retention policies remain relevant; keep the application database as the audit/history source. A free Slack workspace may limit retained history; check its current plan limits. Enable channel notifications in the Slack Android app for mobile alerts; do-not-disturb and phone settings control delivery.

## 2. Create the Slack app

1. Open [Slack Your Apps](https://api.slack.com/apps) and choose **Create New App → From a manifest**.
2. Select your workspace and paste [config/slack-app-manifest.json](../config/slack-app-manifest.json), using JSON mode. Review the requested bot scopes: `chat:write` (send messages), `commands` (`/deals`), and `groups:read` (verify private-channel membership). No channel-history subscription is requested.
3. Confirm **Socket Mode** is enabled and **Interactivity** is enabled. With Socket Mode there is no public Request URL to supply. The manifest registers `/deals`.
4. Under **Basic Information → App-Level Tokens**, generate an app token with `connections:write`.
5. Under **OAuth & Permissions**, install the app to the workspace and obtain the **Bot User OAuth Token**. Workspace policy may require an administrator to approve installation.
6. In the private channel, invite the bot using `/invite` or channel integrations. Invite the intended approving Slack user too.
7. Find the workspace ID and channel ID from the channel's Slack client link (`https://app.slack.com/client/T.../C...`; some private channel IDs start with `G`). Open your Slack profile menu and use **Copy member ID** for the approving user.

Never paste tokens into chat, tracked files, screenshots, Git URLs, or logs. The app-level token normally starts `xapp-`; the bot token normally starts `xoxb-`. The listener validates the workspace and bot through Slack, rather than trusting token prefixes. Public, archived, externally shared/Slack Connect channels, mismatched workspaces, missing bot membership, and absent approving members fail checks before private messages are posted. Those checks run again before each send.

## 3. Configure the always-on host

Install the branch's requirements into the same virtual environment as the app. Store secrets in a protected environment file outside the checkout, or your hosting secret manager. For the Ubuntu VM layout described previously:

```sh
mkdir -p "$HOME/.config/hibid" "$HOME/.local/share/hibid"
chmod 700 "$HOME/.config/hibid" "$HOME/.local/share/hibid"
touch "$HOME/.config/hibid/app.env"
chmod 600 "$HOME/.config/hibid/app.env"
nano "$HOME/.config/hibid/app.env"
```

Keep the existing account hashes, database path and optional ReefAPI key. Add these lines using actual values locally:

```sh
DEAL_HUNTER_USERS='{"alice":"YOUR_EXISTING_APPLICATION_PASSWORD_HASH"}'
DEAL_HUNTER_DB=/home/ubuntu/.local/share/hibid/dealhunter.sqlite3
SLACK_BOT_TOKEN=YOUR_BOT_TOKEN
SLACK_APP_TOKEN=YOUR_APP_LEVEL_TOKEN
SLACK_BINDINGS='[{"owner":"alice","team_id":"T_REPLACE","user_id":"U_REPLACE","channel_id":"C_REPLACE"}]'
```

The ID placeholders intentionally are not valid IDs: replace them with the actual alphanumeric Slack IDs. `owner` must match an existing configured application username. Bindings enforce one owner, one approving Slack identity and one private destination per entry; duplicate identities/destinations are rejected. The app's password hash is not a Slack password. No HiBid, Facebook, Kijiji or Slack user password is collected. The separate listener reads **environment variables**, not Streamlit Secrets. Never change the database path without preserving existing history. For multiple accounts, add separate bindings and private channels to prevent cross-account disclosure.

This listener is a trusted host process: Slack identity replaces browser login for the explicitly mapped account. Restart it immediately after changing/removing account bindings, app account credentials, or tokens; the running process uses the startup configuration. Revoke Slack tokens promptly if compromised. Removing a private-channel member does not remove messages they already read.

Network access requires HTTPS to `slack.com` for Web API/authentication and outbound WSS to the Socket Mode host returned by Slack. On restricted hosts, consult [Slack Socket Mode documentation](https://docs.slack.dev/apis/events-api/using-socket-mode/) for current connection destinations. Don't replace an existing network allowlist blindly. In the Codex environment, configure missing tokens through secure environment settings; supplying them here does not install/configure the production VM.

## 4. Test without external trading actions

```sh
cd "$HOME/hibid-smart-deal-hunter"
.venv/bin/python -m pip install -r requirements.txt
set -a
. "$HOME/.config/hibid/app.env"
set +a
.venv/bin/python -m dealhunter.slack_worker
```

In the configured Slack channel:

- `/deals help` — command reference.
- `/deals status` — current inbox and execution limitations.
- `/deals jobs` or `/deals job 12` — recent job status or complete findings, including inspection flags, research evidence, uncertainty and failures.
- `/deals actions` or `/deals action 7` — list actions or request a current action card.
- `/deals watchlists` — saved watchlist indices.
- `/deals scout 1 123,456` — queue the first saved watchlist using actual numeric ReefAPI category IDs. IDs are required when the watchlist has categories; otherwise the category argument is optional. It queues work only. The existing ten-search-attempt daily budget is enforced when the agent worker runs the scout.
- `/deals note Check the charger before buying` — save a private note visible in the dashboard; `/deals notes` lists recent notes. Notes do not alter existing job inputs or trigger action execution.

Run the existing agent worker separately to process queued jobs:

```sh
.venv/bin/python -m dealhunter.worker --owner alice --loop
```

A completed scout saves candidates and queues their description inspections. Query `/deals job ID` to read all candidate details. Notifications cover newly created actions and terminal jobs created after the listener's first activation for that binding; startup does not broadcast your old history. Ask for older records explicitly using the commands. Agent processing and Slack delivery are separate; the listener never runs pending jobs itself.

## 5. Review and approve on your phone

On an action card, tap **Review and approve**. The modal shows the entire action payload without truncation: lot, hammer/all-in amounts, assumptions, warnings, profit estimates, evidence hashes, or exact platform, ad text, asking price and photo references. An oversized record requires dashboard review; partial text cannot be approved through Slack. Listing text is rendered as plain text, without mentions, link unfurls or embedded remote media.

Check the exact-action box, enter a reason, and tap **Record approval**. A record is approved only if the Slack workspace/user/channel and current action fingerprint still match. Photo rights and received ownership remain mandatory for ad approval. Review saved photo assets in the dashboard before confirming those references; private photos are not uploaded to Slack. Bid approvals expire after ten minutes and ad approvals after 24 hours. Revocation and changes invalidate old buttons; approval/revocation audit records remain in SQLite. An expired/revoked action needs a new proposal.

**Approval is not submission.** No live bidding or personal-marketplace posting adapter is installed, and the Slack listener never calls the executor. Casual replies such as “yes” cannot approve, raise ceilings, change ad text, or submit trades. There is no natural-language autonomous chat model in this listener; use its documented commands. An unsupported command returns help. General channel chatter is not read; no channel-history event permissions are requested.

## 6. Keep the listener running

After successful private testing and when ready to deploy, an example systemd service for the existing Ubuntu layout is below. Adapt the username and directories to your VM; this is an example, not an installed service.

```ini
[Unit]
Description=HiBid private Slack inbox
Wants=network-online.target
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/hibid-smart-deal-hunter
EnvironmentFile=/home/ubuntu/.config/hibid/app.env
ExecStart=/home/ubuntu/hibid-smart-deal-hunter/.venv/bin/python -m dealhunter.slack_worker
Restart=on-failure
RestartSec=15
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Install/start that service only when you intend to deploy. Keep the agent worker as a separate service and back up SQLite consistently. Streamlit Community Cloud sleeping/ephemeral instances are not suitable for durable background Socket Mode service hosting.

## Delivery and troubleshooting

- In the dashboard, expand **Agent team → Slack communication and delivery status** for notes and delivery counts.
- A confirmed Slack message gets status `sent`. Network timeouts/failed responses get `unknown`; a crash during sending can leave `sending`. These are not automatically resent, avoiding duplicate approval alerts. Missing alerts never authorize an action. Request `/deals status`, `/deals job ID` or `/deals action ID` to fetch current state explicitly.
- Slack delivery is bounded to five notifications per binding per polling pass. Older delivery payloads are only notifications: every review checks current database state. Rate limits and network failure can delay or suppress alerts, so the dashboard remains the source of truth.
- If startup fails, verify token type, workspace, scopes, channel privacy, bot invitation, approving member, identity mapping and database access. Raw SDK responses/token values are suppressed in logs. Never enable HTTP/WebSocket debug tracing with real tokens.
- Socket Mode authenticates the outbound connection; there is no HTTP callback endpoint to accept forged public requests. If an HTTP adapter is ever added, it must verify Slack signatures and timestamp freshness separately.
- Automated tests use a fake Slack client and real private SQLite. They do not establish that a real workspace is connected. An end-to-end smoke test requires your tokens and a real private channel; no live message was sent during implementation.
