import argparse
import json
import os
import socket
import sys
import threading
from urllib.parse import urlparse

import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend-origin', required=True)
    args = parser.parse_args()
    origin = urlparse(args.frontend_origin)
    if origin.scheme != 'http' or origin.hostname != '127.0.0.1' or not origin.port or origin.path:
        raise ValueError('Desktop frontend must be a loopback HTTP origin')
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    address = f'http://127.0.0.1:{listener.getsockname()[1]}'
    os.environ['NORN_API_URL'] = address
    os.environ['NORN_FRONTEND_URL'] = args.frontend_origin
    os.environ['NORN_GOOGLE_REDIRECT_URI'] = address + '/'

    class DesktopServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            if self.started:
                print('NORN_DESKTOP_READY ' + json.dumps({'api_url': address, 'pid': os.getpid()}), flush=True)

    server = DesktopServer(uvicorn.Config('app:app', host='127.0.0.1', port=listener.getsockname()[1],
                                          timeout_graceful_shutdown=15))

    def control():
        for line in sys.stdin:
            if line.strip() == 'shutdown':
                break
        server.should_exit = True

    threading.Thread(target=control, daemon=True).start()
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()


if __name__ == '__main__':
    main()