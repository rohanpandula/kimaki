import argparse
import json
import logging
import os
from pathlib import Path
import signal
import uvicorn
from .db import Store


def main():
    parser = argparse.ArgumentParser(description='Opt-in device farm control plane')
    parser.add_argument('mode', choices=['migrate', 'api', 'worker', 'notify'])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.mode == 'migrate':
        Store(os.environ['FARM_DATABASE_URL']).migrate()
    elif args.mode == 'api':
        from .api import create_app
        uvicorn.run(create_app(), host=os.getenv('FARM_BIND', '127.0.0.1'), port=int(os.getenv('FARM_PORT', '8090')), access_log=False)
    elif args.mode == 'notify':
        from .notifier import Notifier
        Notifier(Store(os.environ['FARM_DATABASE_URL']), os.environ['FARM_DISCORD_TOKEN'],
                 json.loads(os.environ['FARM_DISCORD_CHANNELS'])).run()
    elif args.mode == 'worker':
        from .worker import Worker
        worker = Worker(os.environ['FARM_API_URL'], os.environ['FARM_WORKER_TOKEN'],
                        os.getenv('FARM_STATE_DIR', str(Path.home()/'.local/state/kimaki-farm')),
                        adb=os.getenv('FARM_ADB', '/opt/homebrew/bin/adb'),
                        adb_port=int(os.getenv('FARM_ADB_PORT', '5040')))
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: worker.stopping.set())
        worker.run()


if __name__ == '__main__':
    main()
