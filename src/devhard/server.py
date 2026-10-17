"""Foreground single-process ASGI server bound exclusively to IPv4 loopback."""

import json
import signal
import socket
import webbrowser

import uvicorn

from .api import create_app
from .manager import Manager


def serve(store, port=0, open_browser=False):
    with Manager(store, initial_phase='starting') as manager, socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(128)
        url = f'http://127.0.0.1:{listener.getsockname()[1]}'
        manager.api_url = url
        manager.publish('starting')

        def ready():
            manager.publish('ready')
            print(json.dumps({**manager.info, 'workspaces': manager.restored}), flush=True)
            if open_browser:
                webbrowser.open(url)

        config = uvicorn.Config(create_app(manager, url, ready), log_level='warning',
                                proxy_headers=False, access_log=False, server_header=False)
        server = uvicorn.Server(config)
        # Uvicorn handles shutdown, then re-raises the signal to the previous handler.
        # Keep DevHard's established graceful Ctrl-C/SIGTERM exit status of zero.
        previous = {sig: signal.signal(sig, lambda *_: None) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            server.run(sockets=[listener])
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return 0 if server.started else 1
