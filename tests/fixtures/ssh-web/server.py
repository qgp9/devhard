"""Disposable SSH endpoint plus two loopback HTTP apps in one namespace."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading


PORTS = (3000, 6767)


class App(BaseHTTPRequestHandler):
    def do_GET(self):
        identity = os.environ.get('FIXTURE_ID', 'fixture')
        payload = {'fixture_id': identity, 'port': self.server.server_port}
        if self.path.split('?', 1)[0] == '/identity':
            body = json.dumps(payload).encode()
            content_type = 'application/json'
        else:
            safe_id = html.escape(identity, quote=True)
            body = (f'<!doctype html><html data-fixture-id="{safe_id}"><head>'
                    f'<meta charset="utf-8"><title>{safe_id}:{self.server.server_port}</title></head>'
                    f'<body><h1>{safe_id}</h1><p>Loopback app on {self.server.server_port}</p>'
                    '</body></html>').encode()
            content_type = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


def healthcheck():
    for port in (22, *PORTS):
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            pass


def main():
    keys = Path('/run/devhard-keys')
    authorized = Path('/home/devhard/.ssh/authorized_keys')
    authorized.write_bytes((keys / 'authorized_keys').read_bytes())
    authorized.chmod(0o600)
    import pwd
    user = pwd.getpwnam('devhard')
    os.chown(authorized, user.pw_uid, user.pw_gid)
    os.chown(authorized.parent, user.pw_uid, user.pw_gid)
    subprocess.run(['/usr/sbin/sshd', '-t'], check=True)
    servers = [ThreadingHTTPServer(('127.0.0.1', port), App) for port in PORTS]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    ssh = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e'])
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        while not stopped.wait(.2):
            if ssh.poll() is not None:
                raise RuntimeError(f'sshd exited: {ssh.returncode}')
    finally:
        if ssh.poll() is None:
            ssh.terminate()
        ssh.wait(timeout=5)
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    if '--healthcheck' in sys.argv:
        healthcheck()
    else:
        main()
