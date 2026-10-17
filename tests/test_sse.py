import asyncio
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx

from devhard.api import ControlService
from devhard.config import WorkspaceConfig
from devhard.manager import Manager
from devhard.store import Store


class SnapshotStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_only_unchanged_observations_and_disconnect_cleanup(self):
        with tempfile.TemporaryDirectory() as root, Manager(Store(root)) as manager:
            service = ControlService(manager)
            stream = service.events()
            try:
                first = await anext(stream)
                self.assertTrue(first.startswith('event: state\ndata: '))
                queue = next(iter(service.subscribers))
                service.refresh()
                await asyncio.sleep(0)
                self.assertTrue(queue.empty(), 'timestamp-only observations must not emit')
                manager.register(WorkspaceConfig('observed', 'dev1.h'))
                service.refresh()
                observed = await anext(stream)
                self.assertEqual(json.loads(observed.split('data: ', 1)[1])['workspaces'][0]['name'], 'observed')
                peer = service.events()
                await anext(peer)
                self.assertEqual(len(service.subscribers), 2)
                await peer.aclose()
                self.assertEqual(len(service.subscribers), 1)
                with service.lock:
                    for n in range(30):
                        service.snapshot['observation_error'] = str(n)
                        service.notify()
                await asyncio.sleep(0)
                self.assertEqual(queue.qsize(), 1)
                latest = await anext(stream)
                self.assertEqual(json.loads(latest.split('data: ', 1)[1])['observation_error'], '29')
                service.close_streams()
                with self.assertRaises(StopAsyncIteration):
                    await anext(stream)
                self.assertEqual(service.subscribers, {})
            finally:
                await stream.aclose()
                service.close()


class LiveStreamTests(unittest.TestCase):
    def test_auth_multiple_clients_job_updates_reconnect_and_shutdown(self):
        with tempfile.TemporaryDirectory() as root, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            listener.close()
            origin = f'http://127.0.0.1:{port}'
            env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
            child = subprocess.Popen([sys.executable, '-m', 'devhard', '--data-dir', root,
                                      'manager', 'run', '--port', str(port)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                with httpx.Client(base_url=origin, timeout=3) as client:
                    deadline = time.monotonic() + 10
                    while True:
                        try:
                            page = client.get('/')
                            break
                        except httpx.TransportError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(.05)
                    token = re.search(r'name="devhard-token" content="([^"]+)"', page.text)[1]
                    headers = {'Authorization': 'Bearer ' + token, 'Origin': origin}
                    self.assertEqual(client.get('/api/events').status_code, 401)
                    self.assertEqual(client.get('/api/events', headers={**headers, 'Origin': 'https://other.test'}).status_code, 403)

                    def snapshot(lines):
                        for line in lines:
                            if line.startswith('data: '):
                                return json.loads(line[6:])
                        self.fail('stream ended before state')

                    with client.stream('GET', '/api/events', headers=headers) as a, \
                            client.stream('GET', '/api/events', headers=headers) as b:
                        self.assertIn('text/event-stream', a.headers['content-type'])
                        self.assertEqual(a.headers['cache-control'], 'no-store')
                        lines_a, lines_b = a.iter_lines(), b.iter_lines()
                        self.assertEqual(snapshot(lines_a)['workspaces'], [])
                        self.assertEqual(snapshot(lines_b)['workspaces'], [])
                        job = client.post('/api/workspaces', headers=headers,
                                          json=WorkspaceConfig('demo', 'dev1.h').as_dict()).json()
                        for lines in (lines_a, lines_b):
                            while True:
                                state = snapshot(lines)
                                if any(j['id'] == job['id'] and j['state'] == 'succeeded' for j in state['jobs']):
                                    break
                            self.assertEqual(state['workspaces'][0]['name'], 'demo')
                    with client.stream('GET', '/api/events', headers=headers) as reconnect:
                        self.assertEqual(snapshot(reconnect.iter_lines())['workspaces'][0]['name'], 'demo')
                        child.send_signal(signal.SIGTERM)
                        self.assertEqual(child.wait(timeout=5), 0, 'open SSE must not block shutdown')
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
