"""Run queued specialist agents on an always-on host with the same private database."""
import argparse
import os
import time
from pathlib import Path

from dealhunter.agents import run_next_job
from dealhunter.storage import Store, database_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', required=True, help='Existing app username owning the queued jobs')
    parser.add_argument('--loop', action='store_true', help='Poll the queue every five seconds; no automatic bid/publication execution')
    args = parser.parse_args()
    store = Store(database_path(os.environ.get('DEAL_HUNTER_DB'), Path(__file__).resolve().parents[1]))
    while True:
        identifier = run_next_job(store, args.owner, os.environ.get('REEFAPI_KEY', ''))
        if identifier is not None:
            print(f'Processed job #{identifier}; see the private agent inbox for its outcome.', flush=True)
        if not args.loop:
            break
        if identifier is None:
            time.sleep(5)


if __name__ == '__main__':
    main()
