"""Disposable local subprocess for listener and SSH-error-path integration tests."""

import socket
import sys
import time


if sys.argv[1] == 'fail':
    print(sys.argv[2], file=sys.stderr, flush=True)
    raise SystemExit(255)
if sys.argv[1] == 'socks':
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', int(sys.argv[2])))
        server.listen()
        while True:
            stream, _ = server.accept()
            with stream:
                stream.settimeout(.5)
                try:
                    if stream.recv(3):
                        stream.sendall(b'\x05\x00')
                except OSError:
                    pass
else:
    time.sleep(30)
