"""Outbound-only Slack Socket Mode listener; run separately from the agent worker."""
import argparse
import json
import logging
import os
import queue
import time
from pathlib import Path

from dealhunter.slack_bridge import SlackBridge, load_bindings, verify_routes
from dealhunter.storage import Store, database_path


def handle_socket_request(bridge, store, work, socket, request):
    from slack_sdk.socket_mode.response import SocketModeResponse
    payload = request.payload or {}
    if request.type == 'interactive' and payload.get('type') == 'view_submission':
        try:
            result = bridge.interaction(payload)
        except Exception:
            result = {'response_action': 'errors', 'errors': {'reason': 'Approval could not be confirmed. Check the dashboard before retrying.'}}
        socket.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id, payload=result or {}))
        return
    # ACK quickly; processing/Slack API calls happen on the bounded worker queue.
    socket.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
    if request.type not in ('slash_commands', 'interactive'):
        return
    try:
        work.put_nowait((request.type, payload, request.envelope_id))
    except queue.Full:
        # No action was approved; user can check /deals status and retry later.
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    # SDK exceptions/debug bodies can contain private request data. Never enable HTTP debug logs.
    logging.getLogger('slack_sdk').setLevel(logging.CRITICAL)
    logging.getLogger('websocket').setLevel(logging.CRITICAL)
    from slack_sdk import WebClient
    from slack_sdk.socket_mode.websocket_client import SocketModeClient
    try:
        users = json.loads(os.environ.get('DEAL_HUNTER_USERS', '{}'))
        bindings = load_bindings(os.environ.get('SLACK_BINDINGS', ''), users)
        bot_token, app_token = os.environ.get('SLACK_BOT_TOKEN', ''), os.environ.get('SLACK_APP_TOKEN', '')
        if not bot_token or not app_token:
            raise ValueError
        client = WebClient(token=bot_token, retry_handlers=[], timeout=10)
        verify_routes(client, bindings)
        store = Store(database_path(os.environ.get('DEAL_HUNTER_DB'), Path(__file__).resolve().parents[1]), timeout=1)
        bridge = SlackBridge(store, client, bindings)
    except Exception:
        raise SystemExit('Slack startup failed. Check private-channel membership, identity bindings, credentials and storage. No provider details were logged.') from None

    work = queue.Queue(maxsize=100)


    socket = SocketModeClient(app_token=app_token, web_client=client)
    socket.socket_mode_request_listeners.append(lambda connection, request: handle_socket_request(bridge, store, work, connection, request))
    try:
        socket.connect()
        print('Slack listener connected. Exact approvals record intent only; external actions remain disconnected.', flush=True)
        next_poll = 0
        while True:
            try:
                kind, payload, key = work.get(timeout=1)
                try:
                    if kind == 'slash_commands':
                        bridge.command(payload, key)
                    elif store.claim_slack_request('interaction:' + key):
                        bridge.interaction(payload)
                except Exception:
                    print('Slack request could not be completed. Check dashboard state; no automatic external execution.', flush=True)
            except queue.Empty:
                pass
            if time.monotonic() >= next_poll:
                try:
                    bridge.poll()
                except Exception:
                    print('Slack inbox polling failed. Check dashboard and delivery status.', flush=True)
                next_poll = time.monotonic() + 5
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit('Slack connection failed. Check configuration and restart; no raw provider details were logged.') from None
    finally:
        socket.close()


if __name__ == '__main__':
    main()
